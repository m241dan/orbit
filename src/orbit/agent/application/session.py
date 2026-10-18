"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import replace
from typing import assert_never

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.keeper import (
    Clock,
    IdMaker,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.application.lanes import TurnSlot
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.ports import ProposalReview, SessionStore, UsageSource
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.startup_check import AGENT_ID as STARTUP_AGENT
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.switcher import (
    SessionCommand,
    SessionSwitcher,
    SettingsAccess,
    SwitchParts,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.domain.agents import Agent
from orbit.agent.domain.board import WAITING, TaskBoard
from orbit.agent.domain.checks import configure_prompt
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.roles import REVIEW_ROLES, role_task
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    CONFIGURATOR,
    CONFIGURE_CHECKS,
    DEFAULT_WORKSPACE,
    INVALID,
    NOT_CONFIGURED,
    PLANNER,
    PLANNER_LANE,
    SESSIONLESS_EVENTS,
    SUPER_REVIEW,
    UNKNOWN,
    WORK_LANE,
    WRONG_SESSION,
    AcceptTask,
    AddTask,
    CancelAgent,
    CancelTurn,
    Command,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    DecideProposal,
    EditTask,
    Event,
    EventEnvelope,
    FireTrigger,
    Notice,
    RemoveTask,
    ResetConversation,
    RunTasks,
    SendMessage,
    Settings,
    SettingsChanged,
    TaskSpec,
    TurnFinished,
    UpdateSettings,
    VerificationChanged,
)

NO_STEPS_NOTE = "No verification steps configured. Press ctrl+e to set them up."
NO_AGENTS_MESSAGE = "Tasks need agents, and none are set up."
NO_VERIFY_MESSAGE = "Setting up checks needs verification on; orbit was started with --no-verify."
CONFIGURING_NOTE = "A configurator task is already queued or running."
WorkCommand = (
    SendMessage
    | CancelTurn
    | UpdateSettings
    | ResetConversation
    | AddTask
    | RunTasks
    | EditTask
    | AcceptTask
    | RemoveTask
    | FireTrigger
    | CancelAgent
    | DecideProposal
)


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
    tasks. ``PlannerSeat`` holds the planner and its turns; ``WorkLane`` holds the rules of a
    task run: verification and fixes; ``ReviewLane`` runs the reviews; ``SessionSwitcher``
    switches between saved sessions.

    With ``check_at_startup``, the ``verification`` steps also run once in the background as
    the events start, on the base commit; a failure adds a draft task to fix it. With a
    ``usage`` source, the subscription's usage limits are fetched in the background too.

    The session open here is saved in ``store``, if there is one, once it's used; ``clock``
    tells the time, ``new_id`` makes its id and ``name`` names it (by the time, if none). The
    user can open another saved session, or a new one, in its place: one opened has its saved
    settings, but for those ``pinned``, by field name, which keep the ``settings`` given here.
    With ``pick_session``, the saved sessions are listed as the events start, for the user to
    pick one; until they do, the session open is a new one, and what orbit adds to it carries
    over into the one picked. Otherwise, the session open is announced as the events start.
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
        store: SessionStore | None = None,
        clock: Clock = local_now,
        new_id: IdMaker = new_session_id,
        pinned: Collection[str] = (),
        pick_session: bool = False,
        name: str | None = None,
    ) -> None:
        self._settings = settings or Settings()
        self._given = self._settings
        self._pinned = tuple(pinned)
        self._pick_session = pick_session
        self._model_choices = tuple(model_choices)
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._seq = 0  # The last event's place in the stream, counted across sessions.
        self._keeper = SessionKeeper(store, self._events.put, clock, new_id)
        self._keeper.rename(name)
        self._review_count = ReviewCount(store, self._events.put)
        self._tasks = Tasks(TaskBoard(), self._events.put, self._save)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._tasks, self._events, agents)
        self._desk = AgentDesk(agents, self._publish_agents) if agents else None
        self._announced = False
        self._closing = False
        self._lane = self._review_lane()
        self._held = False  # The user cancelled: don't start work by itself.
        hooks = SeatHooks(self._save, self._keeper.use, self._step_names)
        self._seat = PlannerSeat(self._desk, self._tasks, self._events, hooks)
        self._events.put_nowait(self._settings_changed())
        if workspace is not None:
            self._events.put_nowait(Notice(message=f"Workspace: {workspace}"))
        self._announce_steps()
        self._startup = self._startup_check(verification, check_at_startup)
        self._work = self._work_lane()
        post = self._events.put_nowait
        self._work_turns = TurnSlot(WORK_LANE, "Tasks are already running.", post)
        self._usage = _usage_monitor(usage, self._events)
        self._switcher = self._session_switcher(pick_session)

    def _session_switcher(self, picking: bool) -> SessionSwitcher:
        """What switches this session between saved ones, still ``picking`` one if so."""
        parts = SwitchParts(
            self._keeper,
            self._tasks,
            self._seat,
            self._lane,
            self._work,
            lambda: self._work_turns.running,
        )
        access = SettingsAccess(
            lambda: self._settings,
            self._set_settings,
            self._settings_changed,
            self._given,
            self._pinned,
        )
        return SessionSwitcher(parts, access, self._events, picking)

    def _set_settings(self, settings: Settings) -> None:
        """Have the session use ``settings`` from now on."""
        self._settings = settings

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
        task = TaskSpec(prompt=startup_fix_prompt(step, output), settings=settings)
        await self._tasks.change(lambda board: board.add_draft(task))

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def saved_session(self) -> SessionKeeper:
        """The session open here, which is saved as it changes."""
        return self._keeper

    @property
    def pinned(self) -> tuple[str, ...]:
        """The settings, by field name, that a session opened keeps as given here."""
        return self._pinned

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._model_choices

    @property
    def agents(self) -> Agents | None:
        return self._desk.agents if self._desk else None

    @property
    def verification(self) -> Verification | None:
        return self._verifier.verification if self._verifier else None

    async def send(self, envelope: CommandEnvelope) -> None:
        """Handle the command in ``envelope``, then publish its outcome: ``CommandRefused`` if
        it was refused, else ``CommandAccepted``. Other errors escape, with no outcome."""
        command_id = envelope.command_id
        try:
            self._check_addressed(envelope)
            await self._handle(envelope.payload)
        except Refused as refusal:
            refused = CommandRefused(command_id=command_id, code=refusal.code, message=str(refusal))
            await self._events.put(refused)
            return
        await self._events.put(CommandAccepted(command_id=command_id))

    def _check_addressed(self, envelope: CommandEnvelope) -> None:
        """Refuse ``envelope`` unless it's for this workspace and the open session, if any."""
        if envelope.workspace != DEFAULT_WORKSPACE:
            message = f"No workspace {envelope.workspace!r}; the workspace is {DEFAULT_WORKSPACE}."
            raise Refused(UNKNOWN, message)
        session, open_id = envelope.session, self._keeper.id
        if session is not None and session != open_id:
            message = f"Session {session!r} isn't open; the open session is {open_id}."
            raise Refused(WRONG_SESSION, message)

    async def _handle(self, command: Command) -> None:
        if isinstance(command, SessionCommand):
            await self._switcher.handle(command)
        else:
            self._switcher.stay()
            await self._handle_work(command)

    async def _handle_work(self, command: WorkCommand) -> None:
        match command:
            case SendMessage() | CancelTurn() | UpdateSettings():
                await self._handle_prompts(command)
            case AddTask() | RunTasks():
                await self._handle_tasks(command)
            case EditTask() | AcceptTask() | RemoveTask():
                await self._handle_board(command)
            case FireTrigger() | CancelAgent() | DecideProposal() | ResetConversation():
                await self._handle_agents(command)
            case _:
                assert_never(command)

    async def _handle_prompts(self, command: SendMessage | CancelTurn | UpdateSettings) -> None:
        match command:
            case SendMessage():
                await self._send_message(command)
            case CancelTurn(lane=lane):
                await self._cancel_turn(lane)
            case UpdateSettings(settings=settings):
                self._settings = settings
                await self._events.put(self._settings_changed())
                await self._save()
            case _:
                assert_never(command)

    async def _send_message(self, command: SendMessage) -> None:
        """Start a planner turn with the message's text; refused for any other agent."""
        if command.agent != PLANNER:
            message = (
                f"Agent {command.agent!r} can't be messaged; "
                f"the agents you can message are {PLANNER}."
            )
            raise Refused(UNKNOWN, message)
        await self._seat.submit(command.text, self._settings)

    async def _handle_tasks(self, command: AddTask | RunTasks) -> None:
        match command:
            case AddTask(task=task):
                await self._tasks.change(lambda board: board.add(task))
            case RunTasks():
                await self._run_tasks_if_possible()
            case _:
                assert_never(command)

    async def _configure_checks(self) -> None:
        """Queue a configurator first and run it, unless work is running already (then it goes
        next); refused without verification or while one is queued or running."""
        if self._desk is None or self._work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
        if self._verifier is None:
            raise Refused(NOT_CONFIGURED, NO_VERIFY_MESSAGE)
        if self._configuring():
            await self._events.put(Notice(message=CONFIGURING_NOTE))
            return
        task = self._configurator_task(self._desk.agents)
        await self._tasks.change(lambda board: board.add_first(task))
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
                await self._tasks.change(lambda board: board.accept(task_id))
            case RemoveTask(task_id=task_id):
                await self._remove(task_id)
            case _:
                assert_never(command)

    async def _edit(self, task_id: str, task: TaskSpec) -> None:
        """Edit a task, unless it's a review orbit queued: its batch is spent, so it couldn't
        run again. An edited task in a review batch has to finish again before the batch does."""
        if self._lane is not None and self._lane.queued_by_orbit(task_id):
            raise Refused(INVALID, "Reviews can't be edited; remove it instead.")
        await self._tasks.change(lambda board: board.edit(task_id, task))
        if self._lane is not None:
            self._lane.edited(task_id)
            await self._reviews_changed()

    async def _remove(self, task_id: str) -> None:
        """Remove a task, and take it out of its review batch, if any."""
        state = self._board.find(task_id)
        await self._tasks.change(lambda board: board.remove(task_id))
        if self._lane is not None:
            assert state is not None  # The board removed it, so it was there.
            await self._lane.removed(task_id, state)

    async def _run_tasks_if_possible(self) -> None:
        if self._work is None:
            raise Refused(NOT_CONFIGURED, NO_AGENTS_MESSAGE)
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

    async def _cancel_turn(self, lane: str) -> None:
        """Stop the work in ``lane``, or say there's no such lane."""
        if lane == WORK_LANE:
            self._hold_work()
        elif lane == PLANNER_LANE:
            self._seat.cancel()
        else:
            message = f"No lane {lane!r}; the lanes are {WORK_LANE} and {PLANNER_LANE}."
            raise Refused(UNKNOWN, message)

    def _hold_work(self) -> None:
        """Cancel the work lane's run, and hold it until the user asks for work again."""
        self._held = True
        if self._work_turns.cancel():
            assert self._work is not None  # Only the work lane takes work turns.
            self._work.stopped_by_user()

    async def _handle_agents(
        self, command: FireTrigger | CancelAgent | DecideProposal | ResetConversation
    ) -> None:
        match command:
            case CancelAgent(agent_id=agent_id):
                await self._cancel_agent(agent_id)
            case DecideProposal():
                await self._decide(command)
            case FireTrigger():
                await self._fire_trigger(command)
            case ResetConversation():
                await self._reset_conversation(command)
            case _:
                assert_never(command)

    async def _reset_conversation(self, command: ResetConversation) -> None:
        """Reset the planner's conversation; refused for any other agent."""
        if command.agent != PLANNER:
            message = (
                f"Agent {command.agent!r} has no conversation to reset; "
                f"the agents with one are {PLANNER}."
            )
            raise Refused(UNKNOWN, message)
        await self._seat.reset()

    async def _fire_trigger(self, command: FireTrigger) -> None:
        """Run the built-in trigger ``command`` names; refused for any other name, or any input,
        since the built-ins take none."""
        runs = {SUPER_REVIEW: self._super_review, CONFIGURE_CHECKS: self._configure_checks}
        run = runs.get(command.trigger)
        if run is None:
            message = (
                f"No trigger {command.trigger!r}; "
                f"the triggers are {SUPER_REVIEW} and {CONFIGURE_CHECKS}."
            )
            raise Refused(UNKNOWN, message)
        if command.input:
            raise Refused(INVALID, f"The {command.trigger} trigger takes no input.")
        await run()

    async def _decide(self, command: DecideProposal) -> None:
        if not self._proposals.decide(command):
            message = f"No proposal is waiting for task {command.task_id!r}."
            raise Refused(UNKNOWN, message)

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
            raise Refused(UNKNOWN, f"No running agent {agent_id!r}.")
        self._cancel_work_of(agent)

    def _cancel_work_of(self, agent: Agent) -> None:
        """Stop a reviewer's review, or the planner's turn, on its own; else the work lane's."""
        if agent.role in REVIEW_ROLES and self._lane is not None:
            self._lane.cancel(agent.task_id)
        elif agent.role == PLANNER:
            self._seat.cancel()
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
            raise Refused(NOT_CONFIGURED, message)
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
        lane.changed = self._reviews_changed
        desk.suggest = lane.suggest
        return lane

    async def _reviews_changed(self) -> None:
        """Save the review state, which may have changed, and the repository's super review
        count."""
        assert self._lane is not None  # Only the review lane changes it.
        await self._review_count.save(self._lane.planner.since_super)
        await self._save()

    async def _load_review_count(self) -> None:
        """Have the review planner count on from the repository's saved super review count."""
        count = await self._review_count.load()
        if self._lane is not None and count is not None:
            self._lane.planner.since_super = count

    async def _save(self) -> None:
        """Save the session, if it's used, as it is now."""
        await self._keeper.save(self._switcher.contents)

    async def _publish_agents(self, event: Event) -> None:
        """Publish ``event``, from the agents; one that ends a turn adds what the turn cost to
        what the session spent. Publishing never suspends, so a turn is not cancelled between
        its TurnFinished and its own bookkeeping, which saves the session after it."""
        if isinstance(event, TurnFinished) and event.usage is not None:
            self._switcher.spent += event.usage
        await self._events.put(event)

    def _step_names(self) -> tuple[str, ...]:
        """The names of the steps verifying tasks now; none without verification."""
        return self._verifier.step_names if self._verifier else ()

    async def close(self) -> None:
        """Cancel the work in progress, which stops its agent, then stop the startup check, the
        usage monitor, the review lane, the planner, the agents' host and the saving of the
        session, releasing its lock, in that order. Each is stopped even if one before it fails;
        the first failure is raised after."""
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
        parts = (self._startup, self._usage, self._lane, self._seat)
        stops: list[Callable[[], Awaitable[None]]] = [self._stop_turns, self._seat.close_retired]
        stops += [part.close for part in parts if part is not None]
        if self._desk is not None:
            stops.append(self._desk.stop)
        stops.append(self._keeper.close)
        return stops

    async def _stop_turns(self) -> None:
        """Cancel the work lane's and the planner's turns, and wait for them to end."""
        await self._work_turns.close()
        await self._seat.stop_turn()

    async def events(self) -> AsyncIterator[EventEnvelope]:
        if not self._announced:
            self._announced = True
            try:
                await self._load_review_count()
                await self._announce_session()
                await self._announce_leftovers()
            finally:  # A listener cancelled meanwhile mustn't keep the background from starting.
                self._start_background()
        while True:
            event = await self._events.get()
            self._nudge_usage(event)
            yield self._enveloped(event)

    def _enveloped(self, event: Event) -> EventEnvelope:
        """``event`` in its envelope: the next ``seq``, and the open session, unless the event
        belongs to none."""
        self._seq += 1
        session = None if isinstance(event, SESSIONLESS_EVENTS) else self._keeper.id
        return EventEnvelope(seq=self._seq, session=session, payload=event)

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

    async def _announce_session(self) -> None:
        """List the saved sessions to pick from, if asked to, or else announce the one open."""
        if self._pick_session:
            await self._switcher.list_sessions("startup")
        else:
            await self._switcher.announce()

    async def _announce_leftovers(self) -> None:
        """Tell the user about work earlier runs left in the coder's tree."""
        tree = self._desk.agents.tree if self._desk else None
        for note in await tree.leftovers() if tree else []:
            await self._events.put(Notice(message=note))

    def _settings_changed(self) -> SettingsChanged:
        return SettingsChanged(settings=self._settings, model_choices=self._model_choices)
