"""In-process implementation of :class:`orbit.protocol.Session`: the application's core.

It takes commands from the frontend, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import PurePath

from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.handshake import (
    Handshake,
    HandshakeParts,
    ends_stream,
)
from orbit.agent.application.keeper import (
    Clock,
    IdMaker,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.application.outbox import EventSink, Outbox
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.ports import ProposalReview, SessionStore, UsageSource
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.switcher import (
    SessionCommand,
    SessionSwitcher,
    SwitchParts,
)
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.work_commands import CommandParts, WorkCommand, WorkCommands
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.application.work_turns import WorkParts, WorkTurns
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    DEFAULT_WORKSPACE,
    INTERNAL,
    PLANNER_LANE,
    UNKNOWN,
    WORK_LANE,
    WRONG_SESSION,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    Event,
    EventEnvelope,
    Hello,
    RunningAgent,
    SessionSummary,
    Settings,
    Snapshot,
    StepState,
    TaskSpec,
    TurnFinished,
    UsageChanged,
)


def _verifier(
    verification: Verification | None,
    tasks: Tasks,
    events: EventSink,
    agents: Agents | None,
) -> Verifier | None:
    """What checks ``tasks`` (in the agents' tree, if any); none without
    ``verification``."""
    if verification is None:
        return None
    return Verifier(verification, tasks, events.put, agents.tree if agents else None)


def _usage_monitor(usage: UsageSource | None, events: EventSink) -> UsageMonitor | None:
    """What fetches the usage limits from ``usage`` into ``events``; none without a source."""
    return UsageMonitor(usage, events.put) if usage else None


def _reviewed(agents: Agents | None, review: ProposalReview) -> Agents | None:
    """``agents``, with proposals going to ``review`` unless they already go somewhere."""
    if agents is None or agents.proposal_review is not None:
        return agents
    return replace(agents, proposal_review=review)


def _workspace_label(workspace: str | None) -> str | None:
    """The name of the directory of the repository at ``workspace``; none without one."""
    return PurePath(workspace).name if workspace else None


class AgentSession:
    """Takes commands, runs work in the background and publishes its events.

    The session owns the task board and reports it whole on every change. It starts two
    lanes: the planner's, whose turns answer prompts, and the work lane's, which runs the
    tasks. ``PlannerSeat`` holds the planner and its turns; ``WorkLane`` holds the rules of a
    task run: verification and fixes; ``WorkTurns`` decides when it runs; ``ReviewLane`` runs
    the reviews; ``SessionSwitcher`` switches between saved sessions; ``WorkCommands`` handles
    the other commands; ``Handshake`` opens every stream.

    With ``check_at_startup``, the ``verification`` steps also run once in the background as
    the session welcomes the frontend, on the base commit; a failure adds a draft task to fix
    it. With a ``usage`` source, the subscription's usage limits are fetched in the background
    too, from then on.

    Every stream opens with ``Hello``: the session publishes nothing, and refuses every other
    command, until it's answered with ``Welcome``. A ``Catalog`` of what exists follows it, then
    the latest usage if any was fetched, a ``Snapshot`` of the session open and the one-off
    notices: the workspace, no verification steps, the ``startup_notes``, work left by earlier
    runs. Every switch of
    session ends with a ``Catalog`` and a ``Snapshot`` too, and a rename or delete sends a
    ``Catalog``.

    The session open here is saved in ``store``, if there is one, once it's used; ``clock``
    tells the time, ``new_id`` makes its id and ``name`` names it (by the time, if none). The
    user can open another saved session, or a new one, in its place: one opened has its saved
    settings, but for those ``pinned``, by field name, which keep the ``settings`` given here.
    With ``pick_session``, the saved sessions are listed after ``Welcome``, for the user to
    pick one; until they do, the session open is a new one, and what orbit adds to it carries
    over into the one picked; they're listed after the notices.
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
        startup_notes: Sequence[str] = (),
    ) -> None:
        self._settings = SessionSettings(settings or Settings(), pinned, model_choices)
        self._pick_session = pick_session
        self._events = Outbox(lambda: self._keeper.id)
        self._keeper = SessionKeeper(store, self._events.put, clock, new_id)
        self._keeper.rename(name)
        self._review_count = ReviewCount(store, self._events.put)
        self._tasks = Tasks(TaskBoard(), self._events.put, self._save)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._tasks, self._events, agents)
        self._desk = AgentDesk(agents, self._publish_agents) if agents else None
        self._workspace = workspace
        self._startup_notes = tuple(startup_notes)
        self._closing = False
        self._lane = self._review_lane()
        hooks = SeatHooks(self._save, self._keeper.use, self._step_names)
        self._seat = PlannerSeat(self._desk, self._tasks, self._events, hooks)
        self._startup = self._startup_check(verification, check_at_startup)
        self._work = self._work_lane()
        self._turns = self._work_turns()
        self._usage = _usage_monitor(usage, self._events)
        self._switcher = self._session_switcher(pick_session)
        self._commands = self._work_commands()
        self._handshake = Handshake(self._handshake_parts(), self._events)

    def _session_switcher(self, picking: bool) -> SessionSwitcher:
        """What switches this session between saved ones, still ``picking`` one if so."""
        parts = SwitchParts(
            self._keeper,
            self._tasks,
            self._seat,
            self._lane,
            self._work,
            lambda: self._turns.running,
            self._snapshot,
            self._catalog,
        )
        return SessionSwitcher(parts, self._settings, self._events, picking)

    def _work_commands(self) -> WorkCommands:
        """What handles the work commands on this session."""
        parts = CommandParts(
            self._settings,
            self._events,
            self._tasks,
            self._seat,
            self._turns,
            self._lane,
            self._proposals,
            self._desk,
            self._startup,
            self._save,
            self._reviews_changed,
        )
        return WorkCommands(parts)

    def _handshake_parts(self) -> HandshakeParts:
        """What the handshake publishes after ``Welcome``, and starts once it has."""
        return HandshakeParts(
            self._switcher.catalog,
            self._latest_usage,
            self._snapshot,
            self._load_review_count,
            self._workspace,
            self._no_steps,
            self._startup_notes,
            self._leftovers,
            self._list_at_startup if self._pick_session else None,
            self._start_background,
            lambda: self._closing,
        )

    def _work_turns(self) -> WorkTurns:
        """What decides when the work lane runs."""
        parts = WorkParts(
            self._work,
            self._lane,
            self._tasks,
            self._verifier,
            self._desk,
            self._events.put_nowait,
        )
        return WorkTurns(parts)

    def _work_lane(self) -> WorkLane | None:
        if self._desk is None:
            return None
        lane = WorkLane(self._desk, self._tasks, self._verifier, self._lane)
        lane.startup = self._startup
        return lane

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
        return self._settings.current

    @property
    def saved_session(self) -> SessionKeeper:
        """The session open here, which is saved as it changes."""
        return self._keeper

    @property
    def pinned(self) -> tuple[str, ...]:
        """The settings, by field name, that a session opened keeps as given here."""
        return self._settings.pinned

    @property
    def model_choices(self) -> tuple[str, ...]:
        return self._settings.model_choices

    @property
    def agents(self) -> Agents | None:
        return self._desk.agents if self._desk else None

    @property
    def verification(self) -> Verification | None:
        return self._verifier.verification if self._verifier else None

    async def send(self, envelope: CommandEnvelope) -> None:
        """Handle the command in ``envelope``, then publish its outcome: ``CommandRefused`` if
        it was refused, or as ``internal`` if handling it failed, else ``CommandAccepted``, or
        ``Welcome`` for a ``Hello``. Only cancellation escapes, with no outcome. After an
        incompatible ``Hello``, nothing is handled."""
        if self._handshake.ended:
            return
        command_id = envelope.command_id
        try:
            self._check_addressed(envelope)
            command = envelope.payload
            if isinstance(command, Hello):
                await self._handshake.hello(command, command_id)
                return
            self._handshake.admit()
            await self._handle(command)
        except Refused as refusal:
            refused = CommandRefused(command_id=command_id, code=refusal.code, message=str(refusal))
            await self._events.put(refused)
            return
        except Exception as exc:  # A bug in one handler mustn't leave its command unanswered.
            message = f"Orbit failed to handle {type(envelope.payload).__name__}: {exc}"
            await self._events.put(
                CommandRefused(command_id=command_id, code=INTERNAL, message=message)
            )
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

    async def _handle(self, command: SessionCommand | WorkCommand) -> None:
        if isinstance(command, SessionCommand):
            await self._switcher.handle(command)
        else:
            self._switcher.stay()
            await self._commands.handle(command)

    def _agent_on(self, task_id: str) -> str:
        """The agent working on task ``task_id``; only asked while one is."""
        assert self._desk is not None  # Only agents propose.
        return self._desk.working_on(task_id).id

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        planner = ReviewPlanner(desk.agents.review_limits)
        lane = ReviewLane(desk, self._tasks, planner, lambda: self._turns.wake())
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
        await self._switcher.save()

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
        await self._turns.close()
        await self._seat.stop_turn()

    async def events(self) -> AsyncIterator[EventEnvelope]:
        """The events the session publishes, ending after an incompatible ``Hello``'s refusal."""
        while True:
            envelope = await self._events.envelope()
            self._nudge_usage(envelope.payload)
            yield envelope
            if ends_stream(envelope.payload):
                return

    def _start_background(self) -> None:
        """Start the startup check and the usage monitor, if there are any."""
        if self._startup is not None:
            self._startup.start()
        if self._usage is not None:
            self._usage.start()

    def _nudge_usage(self, event: Event) -> None:
        """Have the usage monitor fetch again, if due, once ``event`` ends an agent's turn."""
        if self._usage is not None and isinstance(event, TurnFinished) and event.agent_id:
            self._usage.nudge()

    def _latest_usage(self) -> UsageChanged | None:
        """The usage limits fetched last; none without a usage monitor, or before a fetch."""
        return self._usage.latest if self._usage else None

    def _no_steps(self) -> bool:
        """Whether verification is on with no steps to verify tasks."""
        return self._verifier is not None and not self._verifier.step_names

    async def _list_at_startup(self) -> None:
        """List the saved sessions for the user to pick one."""
        await self._switcher.list_sessions("startup")

    async def _leftovers(self) -> list[str]:
        """Notes on the work earlier runs left in the coder's tree."""
        tree = self._desk.agents.tree if self._desk else None
        return await tree.leftovers() if tree else []

    def _snapshot(self, last_active: datetime | None = None) -> Snapshot:
        """The session open here, last active at ``last_active``, as it stands now."""
        steps, title = self._steps_shown()
        return Snapshot(
            session=self._switcher.summary(last_active),
            settings=self._settings.current,
            model_choices=self._settings.model_choices,
            busy={WORK_LANE: self._turns.running, PLANNER_LANE: self._seat.running},
            tasks=self._board.tasks,
            steps=steps,
            steps_title=title,
            review=self._lane.status if self._lane else None,
            proposals=self._proposals.waiting,
            agents=self._running_agents(),
        )

    def _catalog(self, sessions: tuple[SessionSummary, ...]) -> Catalog:
        """What exists: the workspace holding ``sessions``, and the agents' roles, if any."""
        roles = self._desk.agents.roles if self._desk else ()
        return catalog(_workspace_label(self._workspace), sessions, roles)

    def _steps_shown(self) -> tuple[tuple[StepState, ...], str]:
        """The verification steps the panel shows, and their title: the startup check's while
        it has the panel, else those verifying tasks; none without verification."""
        startup = self._startup.panel if self._startup else None
        if startup is not None:
            return startup.steps, startup.title
        return (self._verifier.progress.steps if self._verifier else ()), ""

    def _running_agents(self) -> tuple[RunningAgent, ...]:
        """The agents running, the startup check first while it runs."""
        startup = self._startup.agent if self._startup else None
        agents = self._desk.running if self._desk else ()
        return agents if startup is None else (startup, *agents)
