"""The application's core: the session the frontends connect to, each connection an
in-process implementation of :class:`orbit.protocol.Session`.

It takes commands from the frontends, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import PurePath

from orbit.agent.application.connection import (
    ConnectionParts,
    Connections,
    Publish,
    SessionConnection,
)
from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.handshake import HandshakeParts
from orbit.agent.application.history import SessionHistory
from orbit.agent.application.keeper import (
    Clock,
    IdMaker,
    NewSessions,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.application.log_writer import Overflow
from orbit.agent.application.outbox import EventSink, Outbox
from orbit.agent.application.ownership import Ownership
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.ports import (
    Fallback,
    ProposalReview,
    RepositoryLock,
    SessionStore,
    UsageSource,
)
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewLane
from orbit.agent.application.session_state import SessionStates, StateParts
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import StartupCheck
from orbit.agent.application.storage_halt import Failure, HaltHooks, StorageHalt
from orbit.agent.application.switcher import (
    SessionCommand,
    SessionSwitcher,
    SwitchParts,
)
from orbit.agent.application.takeover import DECLINED_RETRY_SECONDS, RETRY_SECONDS, Takeover
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.usage_monitor import UsageMonitor
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.views import SessionViews, ViewParts
from orbit.agent.application.work_commands import (
    CommandParts,
    QueuedWork,
    WorkCommand,
    WorkCommands,
)
from orbit.agent.application.work_lane import WorkLane
from orbit.agent.application.work_turns import WorkParts, WorkTurns
from orbit.agent.domain.activity import Activity
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import catalog
from orbit.agent.domain.ownership import WorkspaceOwner, read_only_note
from orbit.agent.domain.refusals import Refused
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    DEFAULT_WORKSPACE,
    INTERNAL,
    PLANNER_LANE,
    UNKNOWN,
    WORK_LANE,
    ActivateSession,
    Attach,
    Catalog,
    CommandAccepted,
    CommandEnvelope,
    CommandRefused,
    DeleteSession,
    Event,
    EventEnvelope,
    Hello,
    History,
    LogGap,
    NewSession,
    Notice,
    OwnershipDeclined,
    OwnershipTaken,
    RenameSession,
    ResumeStorage,
    RunningAgent,
    SessionSummary,
    Settings,
    Snapshot,
    StepState,
    TakeOwnership,
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


_DEFAULT_STORAGE = StorageSettings()

OWNED_NOTE = "This orbit now owns the repository; queued work is running."
"""The notice once a read-only orbit takes the repository over."""


class AgentSession:
    """Takes commands, runs work in the background and publishes its events.

    The session owns the task board and reports it whole on every change. It starts two
    lanes: the planner's, whose turns answer prompts, and the work lane's, which runs the
    tasks. ``PlannerSeat`` holds the planner and its turns; ``WorkLane`` holds the rules of a
    task run: verification and fixes; ``WorkTurns`` decides when it runs; ``ReviewLane`` runs
    the reviews; ``SessionSwitcher`` switches between saved sessions; ``WorkCommands`` handles
    the other commands; ``SessionHistory`` answers ``History``; each ``SessionConnection``
    from :meth:`connect` is a frontend's, whose ``Handshake`` opens its stream. Commands from
    every connection are handled as they arrive.

    With ``check_at_startup``, the ``verification`` steps also run once in the background as
    the session welcomes its first frontend, on the base commit; a failure adds a draft task to
    fix it. With a ``usage`` source, the subscription's usage limits are fetched in the
    background too, from then on.

    Every stream opens with ``Hello``: the session publishes nothing to it, and refuses every
    other command, until it's answered with ``Welcome``. A ``Catalog`` of what exists follows
    it, then the latest usage if any was fetched, a ``Snapshot`` of the session open (or, for a
    ``Hello`` whose ``since`` it can catch up from, the open session's events after it) and the
    one-off notices: the workspace, no verification steps, the ``startup_notes``, work left by
    earlier runs; these reach only the connection whose ``Hello`` they answer, as a
    ``HistoryPage`` does the one asking. Every switch of session ends with a ``Catalog`` and a
    ``Snapshot`` too, and a rename or delete sends a ``Catalog``; each such ``Catalog`` reaches
    every listening connection, so every frontend's picker follows new, renamed and deleted
    sessions, and the ``Snapshot``, the session's new state, every connection viewing it.
    Every connection gets a ``SessionStateChanged`` whenever whether the open session runs
    work, or how many connections view a session, changes: a connection views the open
    session once its greeting is sent, until it closes or ``Attach``\\es to another.
    ``SessionViews`` attaches connections: to a saved session that isn't open, as a read-only
    view whose ``Snapshot`` comes from its record and its log, and where only ``History``,
    ``RenameSession``, ``DeleteSession`` and ``Attach`` work. A connection gets the events of
    the session it views and of none; a session another connection views can't be deleted,
    and once one is, those still attached to it, the one deleting it or one that attached while
    it was being deleted, view the open session, each sent its live ``Snapshot``.
    ``ActivateSession`` and ``NewSession`` switch the open session for the connection sending
    them, which views it live; the others viewing the session left keep it as a read-only view.

    The session open here is saved in ``store``, if there is one, once it's used, and its
    events are kept in the store's log, ``seq`` counting on from the highest there, with their
    text fields cut and each session's log trimmed as the ``storage`` settings say; ``clock``
    tells the time, ``new_id`` makes its id and ``name`` names it (by the time, if none). The
    user can open another saved session, or a new one, in its place: one opened has its saved
    settings, but for those ``pinned``, by field name, which keep the ``settings`` given here.
    With ``pick_session``, the user picks a saved session from their frontend's picker; until
    they do, the session open is a new one, and what orbit adds to it carries over into the one
    picked.

    When a write to the store fails, the session halts: the agents running, the planner's turn,
    the reviews and the startup check are cancelled, an ``ErrorOccurred`` coded
    ``storage_failed`` explains why, and every command but ``Hello`` and ``ResumeStorage`` is
    refused so. Nothing is written to the store from then on: the events not logged go to the
    ``fallback`` instead. ``ResumeStorage`` retries the writes; once they work, the events
    parked are logged, a ``LogGap`` is logged for each span of those lost, and the halt is
    lifted.

    With an ``owner``, another orbit owns the repository and this one is read-only: its
    ``Catalog`` and ``Snapshot`` say so, a notice naming the owner comes first among the
    startup notes, ``RunTasks`` and ``FireTrigger`` are accepted and queued rather than run, and
    no work starts by itself: neither the startup check nor reviews run. It notes no leftovers,
    since only the owner can tell its own running work from what earlier runs left. Given the
    repository's ``lock``, it's tried every ``lock_retry`` seconds from the first ``Welcome``;
    once it's free, it's held and offered with ``OwnershipOffered``. ``TakeOwnership`` answers:
    taking it publishes ``OwnershipTaken`` and a notice, then runs the work queued, in order;
    declining it publishes ``OwnershipDeclined``, lets the lock go and offers it again once
    another orbit has held it since, or after ``lock_declined_retry`` seconds.
    While the offer is open, ``Snapshot`` says so.
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
        storage: StorageSettings = _DEFAULT_STORAGE,
        fallback: Fallback | None = None,
        owner: WorkspaceOwner | None = None,
        lock: RepositoryLock | None = None,
        lock_retry: float = RETRY_SECONDS,
        lock_declined_retry: float = DECLINED_RETRY_SECONDS,
    ) -> None:
        self._ownership = Ownership(read_only=owner is not None)
        waiting_on = lock if self._ownership.read_only else None
        self._takeover = Takeover(
            waiting_on, lambda event: self._events.put(event), lock_retry, lock_declined_retry
        )
        self._settings = SessionSettings(settings or Settings(), pinned, model_choices)
        self._storage = storage
        halt_hooks = HaltHooks(
            lambda: self._events.stop_log(self._keeper.id), self._stop_work, self._retry_storage
        )
        self._halt = StorageHalt(lambda event: self._events.put(event), halt_hooks)
        overflow = Overflow(fallback, self._log_failed)
        self._connections = Connections(lambda: self._states.check(), lambda: self._keeper.id)
        self._events = Outbox(lambda: self._keeper.id, store, storage, overflow)
        self._events.deliver = self._deliver
        new_sessions = NewSessions(clock, new_id, self._events.forget, self._events.catch_up)
        self._keeper = SessionKeeper(store, self._events.put, self._halt, new_sessions)
        self._keeper.rename(name)
        self._review_count = ReviewCount(store, self._halt, self._events.put)
        self._tasks = Tasks(TaskBoard(), self._events.put, self._save)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._tasks, self._events, agents)
        self._desk = AgentDesk(agents, self._publish_agents) if agents else None
        self._workspace = workspace
        owner_notes = () if owner is None else (read_only_note(owner),)
        self._startup_notes = (*owner_notes, *startup_notes)
        self._closing = False
        self._started = False
        self._lane = self._review_lane()
        hooks = SeatHooks(self._save, self._keeper.use, self._step_names)
        self._seat = PlannerSeat(self._desk, self._tasks, self._events, hooks)
        self._startup = self._startup_check(
            verification, check_at_startup and not self._ownership.read_only
        )
        self._work = self._work_lane()
        self._turns = self._work_turns()
        self._usage = _usage_monitor(usage, self._events)
        self._switcher = self._session_switcher(pick_session)
        self._commands = self._work_commands()
        self._greeting = self._handshake_parts()
        self._history = SessionHistory(self._keeper, self._events)
        self._states = self._session_states()
        self._views = self._session_views()
        self._watch_background()

    async def _retry_storage(self) -> Failure | None:
        """Retry the writes to the store a halt stopped: the session record as it is now and
        the review count, then appending to the event log, which logs a ``LogGap`` for each
        span of events lost; returns the write that failed and its error, if one did."""
        return await self._retry_saves() or await self._resume_log()

    async def _retry_saves(self) -> Failure | None:
        """Save the session record as it is now, and a saved session's rename whose save failed,
        then the review count, though halted; returns the one that failed and its error, if one
        did."""
        record = await self._keeper.retry(self._switcher.contents)
        if record is not None:
            return FailedWrite.SESSION_RECORD, record
        count = await self._review_count.retry()
        return None if count is None else (FailedWrite.REVIEW_COUNT, count)

    async def _resume_log(self) -> Failure | None:
        """Append to the event log again, publishing a ``LogGap`` for each span of events that
        reached neither it nor a fallback file; returns the error, if appending failed."""
        resumed = await self._events.resume_log()
        if isinstance(resumed, RuntimeError):
            return FailedWrite.EVENT_LOG, resumed
        for first, last in resumed:
            await self._events.put(LogGap(first_seq=first, last_seq=last))
        return None

    def _log_failed(self, error: RuntimeError) -> None:
        """Halt the session, as appending to its event log failed with ``error``."""
        self._halt.fail(FailedWrite.EVENT_LOG, error)

    def _session_states(self) -> SessionStates:
        """What publishes the open session's state as it changes."""
        parts = StateParts(
            lambda: self._keeper.id,
            self._activity,
            self._connections,
            self._events.put_nowait,
        )
        return SessionStates(parts)

    def _session_views(self) -> SessionViews:
        """What attaches connections to sessions."""
        parts = ViewParts(
            self._keeper,
            self._events,
            self._connections,
            self._states,
            self._settings,
            self._snapshot,
            self._ownership,
        )
        return SessionViews(parts)

    def _watch_background(self) -> None:
        """Have the open session's state checked whenever the reviews or the startup check stop,
        as nothing is published then."""
        for part in (self._lane, self._startup):
            if part is not None:
                part.on_stopped(self._states.check)

    def _session_switcher(self, picking: bool) -> SessionSwitcher:
        """What switches this session between saved ones, still ``picking`` one if so."""
        parts = SwitchParts(
            self._keeper,
            self._tasks,
            self._seat,
            self._lane,
            self._work,
            self._activity,
            self._snapshot,
            self._catalog,
            lambda catalog: self._connections.broadcast(self._events.stamp(catalog)),
            lambda kept: self._views.switching(kept),
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
            self._ownership,
        )
        return WorkCommands(parts)

    def _handshake_parts(self) -> HandshakeParts:
        """What the handshake publishes after ``Welcome``, and starts once it has."""
        return HandshakeParts(
            self._switcher.catalog,
            self._latest_usage,
            self._snapshot,
            lambda since: self._events.missed(self._keeper.id, since),
            self._load_review_count,
            self._workspace,
            self._no_steps,
            self._startup_notes,
            self._leftovers,
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
            lambda: self._halt.halted,
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

    @property
    def read_only(self) -> bool:
        """Whether another orbit owns the repository, so the work asked for is queued."""
        return self._ownership.read_only

    @property
    def storage_halted(self) -> bool:
        """Whether a write to the store failed, so the session is halted until resumed."""
        return self._halt.halted

    @property
    def queued_work(self) -> tuple[QueuedWork, ...]:
        """The commands that start work, queued while read-only, oldest first."""
        return self._commands.queued

    def connect(self) -> SessionConnection:
        """A new connection to this session, with its own handshake and stream."""
        parts = ConnectionParts(
            self._greeting, self._events, self._events.stamp, self._connections, self._answer
        )
        return SessionConnection(parts)

    async def open_latest(self) -> None:
        """Open the most recently active saved session no other orbit has open in place of the
        new one, if there's one that can be opened, as the user would; the new one stays open
        if it can't be."""
        for summary in await self._keeper.summaries():
            if not summary.locked and summary.unreadable is None:
                with suppress(Refused):
                    await self._switcher.handle(ActivateSession(session=summary.id))
                return

    async def _answer(
        self, envelope: CommandEnvelope, connection: SessionConnection, publish: Publish
    ) -> None:
        """Handle the command in ``envelope``, from ``connection``, then ``publish`` its
        outcome: ``CommandRefused`` if it was refused, as it is but for ``Hello`` and
        ``ResumeStorage`` while the session is halted, or but for those a view lets through
        while the connection views a session that isn't open, or as ``internal`` if handling it
        failed, else ``CommandAccepted``, or ``Welcome`` for a ``Hello``. Only cancellation
        escapes, with no outcome."""
        command_id = envelope.command_id
        handshake = connection.handshake
        try:
            self._check_addressed(envelope, connection)
            command = envelope.payload
            if isinstance(command, Hello):
                await handshake.hello(command, command_id)
                return
            handshake.admit()
            self._views.admit(connection, command)
            await self._admitted(command, command_id, connection)
        except Refused as refusal:
            await publish(
                CommandRefused(command_id=command_id, code=refusal.code, message=str(refusal))
            )
            return
        except Exception as exc:  # A bug in one handler mustn't leave its command unanswered.
            message = f"Orbit failed to handle {type(envelope.payload).__name__}: {exc}"
            await publish(CommandRefused(command_id=command_id, code=INTERNAL, message=message))
            return
        await publish(CommandAccepted(command_id=command_id))

    def _check_addressed(self, envelope: CommandEnvelope, connection: SessionConnection) -> None:
        """Refuse ``envelope`` unless it's for this workspace and the session ``connection``
        views, if any."""
        if envelope.workspace != DEFAULT_WORKSPACE:
            message = f"No workspace {envelope.workspace!r}; the workspace is {DEFAULT_WORKSPACE}."
            raise Refused(UNKNOWN, message)
        self._views.check_addressed(connection, envelope.session)

    async def _admitted(
        self,
        command: SessionCommand | WorkCommand | History | ResumeStorage | TakeOwnership | Attach,
        command_id: str,
        connection: SessionConnection,
    ) -> None:
        """Handle ``command``, sent as ``command_id`` from ``connection`` once the handshake is
        done: refused while the session is halted, but for ``ResumeStorage``, which resumes
        it."""
        if isinstance(command, ResumeStorage):
            await self._halt.resume()
            return
        self._halt.check()
        if isinstance(command, Attach):
            await self._views.attach(connection, command.session)
            return
        if isinstance(command, ActivateSession | NewSession):
            await self._activate(command, connection)
            return
        await self._handle(command, command_id)

    async def _activate(
        self, command: ActivateSession | NewSession, connection: SessionConnection
    ) -> None:
        """Make the session ``command`` names, or a new one, the open one, viewed live by
        ``connection``, then give the other connections the snapshots of what they view now:
        the session left a read-only view. Activating the open session attaches to it."""
        if isinstance(command, ActivateSession) and command.session == self._keeper.id:
            await self._views.attach(connection, command.session)
            return
        # The session left is the one the switch left, as others' switches may run meanwhile.
        left = await self._switcher.activate(command)
        if left is not None:
            await self._views.activated(left)

    async def _handle(
        self, command: SessionCommand | WorkCommand | History | TakeOwnership, command_id: str
    ) -> None:
        if isinstance(command, History):
            await self._events.put(await self._history.page(command))
        elif isinstance(command, DeleteSession):
            await self._views.check_deletable(command.session_id)
            await self._switcher.handle(command)
            await self._views.deleted(command.session_id)
        elif isinstance(command, RenameSession):
            await self._switcher.handle(command)
            self._views.renamed(command.session_id, command.name)
        elif isinstance(command, TakeOwnership):
            await self._take_ownership(command, command_id)
        elif isinstance(command, SessionCommand):
            await self._switcher.handle(command)
        else:
            self._switcher.stay()
            await self._commands.handle(command, command_id)

    async def _take_ownership(self, command: TakeOwnership, command_id: str) -> None:
        """Answer the offer of the repository: own it, if ``command`` takes it, and run the work
        queued for ``command_id`` and the reviews waiting; refused when no offer is open."""
        self._takeover.answer(command.take)
        if not command.take:
            await self._events.put(OwnershipDeclined())
            return
        self._ownership.own()
        await self._events.put(OwnershipTaken())
        await self._events.put(Notice(message=OWNED_NOTE))
        await self._commands.own(command_id)
        if self._lane is not None:  # After the queued work, so its tasks get agents first.
            self._lane.kick()

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
        lane.ownership = self._ownership
        lane.halted = lambda: self._halt.halted
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
        """Stop a storage halt under way and the tries of the repository's lock, cancel the
        work in progress, which stops its agent, then stop the startup check, the usage
        monitor, the review lane, the planner, the agents' host, the saving of the session,
        releasing its lock, and the writing of its events to the store, then end every
        connection's stream, in that order. Each is stopped even if one before it fails; the
        first failure is raised after."""
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
        stops: list[Callable[[], Awaitable[None]]] = [
            self._halt.close,
            self._takeover.close,
            self._stop_turns,
            self._seat.close_retired,
        ]
        stops += [part.close for part in parts if part is not None]
        if self._desk is not None:
            stops.append(self._desk.stop)
        stops += [self._keeper.close, self._events.close, self._end_connections]
        return stops

    async def _end_connections(self) -> None:
        """End every connection's stream once what it has queued is read."""
        self._connections.end()

    async def _stop_turns(self) -> None:
        """Cancel the work lane's and the planner's turns, and wait for them to end."""
        await self._turns.close()
        await self._seat.stop_turn()

    async def _stop_work(self) -> None:
        """Cancel the reviews and the startup check, then the work lane's and the planner's
        turns, as the session halts, and wait for them to end; nothing that could wake the work
        lane runs once its turn is cancelled."""
        for part in (self._lane, self._startup):
            if part is not None:
                await part.close()
        await self._stop_turns()

    def _deliver(self, envelope: EventEnvelope) -> None:
        """Pass ``envelope``, just published, to the connections it's for, then publish the open
        session's state if that changed."""
        self._nudge_usage(envelope.payload)
        self._connections.deliver(envelope)
        self._states.check()

    def _activity(self) -> Activity:
        """What's in progress now: a task run, a review, a planner turn or the startup check."""
        return Activity(
            tasks=self._turns.running,
            review=self._lane is not None and self._lane.running,
            planner=self._seat.running,
            startup_check=self._startup is not None and self._startup.running,
        )

    def _start_background(self) -> None:
        """Start the startup check and the usage monitor, if there are any, and the tries of
        the repository's lock, unless they were started for an earlier connection."""
        if self._started:
            return
        self._started = True
        self._takeover.start()
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

    async def _leftovers(self) -> list[str]:
        """Notes on the work earlier runs left in the coder's tree; none while read-only, as
        the owner's live work would look left over."""
        tree = self._desk.agents.tree if self._desk and not self._ownership.read_only else None
        return await tree.leftovers() if tree else []

    async def _snapshot(self, last_active: datetime | None = None) -> Snapshot:
        """The session open here, last active at ``last_active``, as it stands now, with its
        latest events."""
        transcript = await self._events.transcript(self._keeper.id)
        # Read after the transcript, so the state is as it stands once that's read.
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
            transcript=transcript,
            storage_halt=self._halt.message,
            read_only=self._ownership.read_only,
            ownership_offered=self._takeover.offered,
            open_session=self._views.open_session(),
            viewers=self._states.of(self._keeper.id).viewers,
        )

    def _catalog(self, sessions: tuple[SessionSummary, ...]) -> Catalog:
        """What exists: the workspace holding ``sessions``, the open one's state as a connection
        joining would see it, and the agents' roles, if any."""
        roles = self._desk.agents.roles if self._desk else ()
        label = _workspace_label(self._workspace)
        live = self._states.current(joining=True).values()
        return catalog(label, sessions, roles, read_only=self._ownership.read_only, live=live)

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
