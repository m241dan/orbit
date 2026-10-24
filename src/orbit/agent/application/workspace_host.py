"""The application's core: one workspace's backend, which the control plane routes the
frontends' commands to.

It takes commands from the frontends, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import PurePath

from orbit.agent.application.closing import close_all
from orbit.agent.application.coder_slots import CoderSeat, CoderSlots
from orbit.agent.application.connection import Connections, Place, SessionConnection
from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.handover import OWNED_NOTE as OWNED_NOTE
from orbit.agent.application.handover import Handover, HandoverParts
from orbit.agent.application.handshake import HostGreeting
from orbit.agent.application.history import SessionHistory
from orbit.agent.application.keeper import (
    AlreadyOpen,
    Clock,
    IdMaker,
    NewSessions,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.application.lanes import Background, SessionGate
from orbit.agent.application.log_writer import Overflow
from orbit.agent.application.outbox import EventSink, Outbox, Shown
from orbit.agent.application.planner_seat import PlannerSeat, SeatHooks
from orbit.agent.application.ports import (
    Fallback,
    ProposalReview,
    RepositoryLock,
    SessionStore,
    StoreFailed,
    UsageLedger,
)
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.session_state import SessionStates, StateParts
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import CheckSite, StartupCheck
from orbit.agent.application.storage_halt import Failure, HaltHooks, StorageHalt, retried
from orbit.agent.application.switcher import (
    SessionCommand,
    SessionSwitcher,
    SwitchParts,
)
from orbit.agent.application.takeover import (
    DECLINED_RETRY_SECONDS,
    RETRY_SECONDS,
    RetryTimes,
    Takeover,
)
from orbit.agent.application.task_trace import TaskTracing
from orbit.agent.application.tasks import Tasks
from orbit.agent.application.usage_recorder import UsageRecorder
from orbit.agent.application.verifier import Verification, Verifier
from orbit.agent.application.views import SessionViews, ViewParts
from orbit.agent.application.work_commands import (
    CommandParts,
    QueuedWork,
    WorkCommand,
    WorkCommands,
)
from orbit.agent.application.work_lane import Beside, WorkLane
from orbit.agent.application.work_turns import WorkParts, WorkTurns
from orbit.agent.domain.activity import Activity
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import workspace_entry
from orbit.agent.domain.ownership import WorkspaceOwner, queued_dropped_note, read_only_note
from orbit.agent.domain.refusals import STOPPING, Refused
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.roles import role_task
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.storage_halt import FailedWrite
from orbit.agent.domain.verification import VerificationStep, startup_fix_prompt
from orbit.protocol import (
    CODER,
    INVALID,
    PLANNER_LANE,
    WORK_LANE,
    ActivateSession,
    Attach,
    DeleteSession,
    Event,
    History,
    LogGap,
    NewSession,
    Notice,
    RenameSession,
    ResumeStorage,
    RunningAgent,
    SessionSummary,
    Settings,
    StepState,
    TakeOwnership,
    TaskSpec,
    TurnFinished,
    Usage,
    UsageChanged,
    WorkspaceEntry,
)
from orbit.tracing import NoTelemetry, Telemetry


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


def _reviewed(agents: Agents | None, review: ProposalReview) -> Agents | None:
    """``agents``, with proposals going to ``review`` unless they already go somewhere."""
    if agents is None or agents.proposal_review is not None:
        return agents
    return replace(agents, proposal_review=review)


def _workspace_label(workspace: str | None) -> str | None:
    """The name of the directory of the repository at ``workspace``; none without one."""
    return PurePath(workspace).name if workspace else None


_DEFAULT_STORAGE = StorageSettings()
_NO_TELEMETRY = NoTelemetry()

UNREGISTERED_WORKSPACE = "unregistered"
"""The id of a host's workspace when it's given none, as one with no store isn't registered."""


async def _unannounced(entry: WorkspaceEntry) -> None:
    """Tell no one of ``entry``, as a host no control plane serves has no one to tell."""


async def _nothing_parked() -> Sequence[str]:
    """No notes, as a host given no fallback files to recover recovers none."""
    return ()


class WorkspaceHost:
    """One workspace's backend: takes the commands the control plane routes to it, runs work in
    the background and publishes its events.

    The session owns the task board and reports it whole on every change. It starts two
    lanes: the planner's, whose turns answer prompts, and the work lane's, which runs the
    tasks. ``PlannerSeat`` holds the planner and its turns; ``WorkLane`` holds the rules of a
    task run: verification and fixes; ``WorkTurns`` decides when it runs; ``ReviewLane`` runs
    the reviews; ``SessionSwitcher`` switches between saved sessions; ``WorkCommands`` handles
    the other commands; ``SessionHistory`` answers ``History``. Its ``Outbox`` stamps its
    events, in envelopes naming the workspace by ``workspace_id``, and its ``Connections`` route
    them among the connections the control plane makes, whose ``Handshake`` greets each with
    the :attr:`greeting` as it first attaches here. Commands from every connection are handled
    as they arrive. Its catalog entry says it's ``managed`` if it's a clone orbit made.

    With ``check_at_startup``, the ``verification`` steps also run once in the background as
    its background work starts, on the base commit; a failure adds a draft task to
    fix it.

    A coder's or configurator's task takes one of :attr:`coder_slots` as it starts, waiting in
    line for one while every one is taken, and gives it back once it ends; the control plane
    serving the host shares its slots across its workspaces.

    The one-off notices of a greeting are the workspace, no verification steps, the
    ``startup_notes`` and work left by earlier runs; like the rest of a greeting, they reach
    only the connection whose first ``Attach`` they answer, as a ``HistoryPage`` does the one
    asking.
    Every switch of session ends with a ``Catalog`` and a
    ``Snapshot`` too, and a rename or delete sends a ``Catalog``: :attr:`catalog_changed` is
    given the workspace's entry in it, and the control plane sends the ``Catalog`` to
    every listening connection, so every frontend's picker follows new, renamed and deleted
    sessions, and the ``Snapshot``, the session's new state, every connection viewing it.
    Every connection gets a ``SessionStateChanged`` whenever whether the open session runs
    work, or how many connections view a session, changes: a connection views the session it
    attaches to once its greeting is sent, until it closes or ``Attach``\\es to another.
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
    tells the time, ``new_id`` makes its id and ``name`` names it (by the time, if none). Each
    of its agents' turns that ends with usage is recorded in the ``ledger``, if there is one,
    in the background, with the session, the agent's role and model, and the time, and
    :attr:`usage_recorded` is called once its row is written. Its work is traced with
    ``telemetry``, recording nothing by default. The
    user can open another saved session, or a new one, in its place: one opened has its saved
    settings, but for those ``pinned``, by field name, which keep the ``settings`` given here.
    With ``pick_session``, the user picks a saved session from their frontend's picker; until
    they do, the session open is a new one, and what orbit adds to it carries over into the one
    picked.

    When a write to the store fails, the session halts: the agents running, the planner's turn,
    the reviews and the startup check are cancelled, an ``ErrorOccurred`` coded
    ``storage_failed`` explains why, and every command but ``Hello``, a connection's first
    ``Attach`` and ``ResumeStorage`` is refused so. Nothing is written to the store from then
    on: the events not logged go to the ``fallback`` instead. ``ResumeStorage`` retries the
    writes; once they work, the events parked are logged, a ``LogGap`` is logged for each span
    of those lost, and the halt is lifted.

    With an ``owner``, another orbit owns the repository and this one is read-only: its
    ``Catalog`` and ``Snapshot`` say so, a notice naming the owner comes first among the
    startup notes, ``RunTasks`` and ``FireTrigger`` are accepted and queued rather than run, and
    no work starts by itself: neither the startup check nor reviews run. It notes no leftovers,
    since only the owner can tell its own running work from what earlier runs left. Given the
    repository's ``lock``, ``Takeover`` offers it once it's free, trying it every ``lock_retry``
    seconds and after a decline waiting up to ``lock_declined_retry``, and ``Snapshot`` says
    while the offer is open; ``Handover`` answers ``TakeOwnership``, calling ``owned`` first.
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
        workspace_id: str = UNREGISTERED_WORKSPACE,
        store: SessionStore | None = None,
        clock: Clock = local_now,
        new_id: IdMaker = new_session_id,
        pinned: Collection[str] = (),
        pick_session: bool = False,
        name: str | None = None,
        startup_notes: Sequence[str] = (),
        storage: StorageSettings = _DEFAULT_STORAGE,
        fallback: Fallback | None = None,
        recover: Callable[[], Awaitable[Sequence[str]]] = _nothing_parked,
        owner: WorkspaceOwner | None = None,
        lock: RepositoryLock | None = None,
        lock_retry: float = RETRY_SECONDS,
        lock_declined_retry: float = DECLINED_RETRY_SECONDS,
        managed: bool = False,
        ledger: UsageLedger | None = None,
        telemetry: Telemetry = _NO_TELEMETRY,
    ) -> None:
        # What its work will be traced with.
        self.telemetry = telemetry
        self.owned: Callable[[], None] = lambda: None
        # The usage limits fetched last, which the control plane serving this host knows.
        self.latest_usage: Callable[[], UsageChanged | None] = lambda: None
        self.catalog_changed: Callable[[WorkspaceEntry], Awaitable[None]] = _unannounced
        self.usage_recorded: Callable[[], None] = lambda: None
        # The coders' slots its tasks take, shared by the control plane serving this host.
        self.coder_slots = CoderSlots()
        self._takeover = Takeover(
            owner is not None,
            lock,
            lambda event: self._events.put(event),
            RetryTimes(lock_retry, lock_declined_retry),
        )
        self._settings = SessionSettings(settings or Settings(), pinned, model_choices)
        self._storage = storage
        halt_hooks = HaltHooks(
            lambda: self._events.stop_log(self._keeper.id), self._stop_work, self._retry_storage
        )
        self._halt = StorageHalt(lambda event: self._events.put(event), halt_hooks)
        self._gate = SessionGate(
            lambda: self._halt.halted, lambda: self._takeover.read_only, self._halt.check
        )
        overflow = Overflow(fallback, self._log_failed)
        self._connections = Connections(lambda: self._states.check(), lambda: self._keeper.id)
        self._workspace_id = workspace_id
        self._events = Outbox(lambda: self._keeper.id, store, storage, overflow)
        self._events.workspace = workspace_id
        new_sessions = NewSessions(clock, new_id, self._events.forget, self._events.catch_up)
        self._keeper = SessionKeeper(store, self._events.put, self._halt, new_sessions)
        self._keeper.rename(name)
        self._review_count = ReviewCount(store, self._halt, self._events.put)
        self._usage = UsageRecorder(ledger, self._halt, self._events.put, clock)
        self._usage.recorded = lambda: self.usage_recorded()
        self._count_loader = Background("review count", self._events.put)
        self._tasks = Tasks(TaskBoard(), self._events.put, self._save)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(agents, self._proposals)
        self._verifier = _verifier(verification, self._tasks, self._events, agents)
        self._desk = AgentDesk(agents, self._publish_agents) if agents else None
        self._workspace = workspace
        self._managed = managed
        self._recover = recover
        self._owner = owner
        owner_notes = () if owner is None else (read_only_note(owner),)
        self._startup_notes = (*owner_notes, *startup_notes)
        self._closing = False
        self._started = False
        self._lane = self._review_lane()
        self._coders = CoderSeat(lambda: self.coder_slots, workspace_id, lambda: self._keeper.id)
        hooks = SeatHooks(self._save, self._keeper.use, self._step_names, self._halt.check)
        self._seat = PlannerSeat(self._desk, self._tasks, self._events, hooks)
        self._startup = self._startup_check(
            verification, check_at_startup and not self._takeover.read_only
        )
        self._work = self._work_lane()
        self._turns = self._work_turns()
        self._switcher = self._session_switcher(pick_session)
        self._commands = self._work_commands()
        self._handover = self._make_handover()
        self._greeting = self._host_greeting()
        self._history = SessionHistory(self._keeper, self._events)
        self._states = self._session_states()
        self._views = self._session_views()
        self._watch_background()

    async def _retry_storage(self) -> Failure | None:
        """Retry the writes to the store a halt stopped: the session record as it is now, with a
        saved session's rename whose save failed, the review count and the usage, then appending
        to the event log, which logs a ``LogGap`` for each span of events lost; returns the write
        that failed and its error, if one did."""
        record = self._keeper.retried(self._switcher.contents)
        saves = await retried((record, self._review_count, self._usage))
        return saves or await self._resume_log()

    async def _resume_log(self) -> Failure | None:
        """Append to the event log again, publishing a ``LogGap`` for each span of events that
        reached neither it nor a fallback file; returns the error, if appending failed."""
        resumed = await self._events.resume_log()
        if isinstance(resumed, StoreFailed):
            return FailedWrite.EVENT_LOG, resumed
        for first, last in resumed:
            await self._events.put(LogGap(first_seq=first, last_seq=last))
        return None

    def _log_failed(self, error: StoreFailed) -> None:
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
            self._halt,
            self._takeover,
        )
        return SessionViews(parts)

    def _watch_background(self) -> None:
        """Have the open session's state checked whenever the reviews or the startup check stop,
        as nothing is published then: the reviews make it running, and the startup check is
        among the activity it reports."""
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
            self._entry,
            lambda entry: self.catalog_changed(entry),
            self._switching,
        )
        return SessionSwitcher(parts, self._settings, self._events, picking)

    def _switching(self, kept: bool) -> None:
        """A switch is under way, with whether the session left stays ``kept``: its views
        follow, and its run, if the usage limit stopped one, no longer starts again, nor waits in
        line for a coder; the work queued while read-only is dropped, with a note if any was."""
        self._views.switching(kept)
        self._turns.forget_wait()
        self._coders.drop()
        dropped = self._commands.drop_queued()
        if dropped:
            self._events.put_nowait(Notice(message=queued_dropped_note(dropped)))

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
            self._gate,
        )
        return WorkCommands(parts)

    def _make_handover(self) -> Handover:
        """What answers the offer of the repository."""
        parts = HandoverParts(
            self._takeover,
            self._commands,
            self._events,
            lambda: self.owned(),
            lambda: self._switcher.publish_catalog(),
            self._recover,
            self._recount_reviews,
            self._kick_reviews,
        )
        return Handover(parts)

    def _kick_reviews(self) -> None:
        """Start the reviews waiting, if there's a review lane, unless closing."""
        if self._lane is not None and not self._closing:
            self._lane.kick()

    def _host_greeting(self) -> HostGreeting:
        """What the handshake of a connection publishes of this host as it first attaches."""
        return HostGreeting(
            lambda: self._keeper.id,
            lambda since: self._events.missed(self._keeper.id, since),
            self._workspace,
            self._no_steps,
            self._startup_notes,
            self._leftovers,
            self.start_background,
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
            self._gate,
            lambda: self.latest_usage(),
        )
        return WorkTurns(parts)

    def _work_lane(self) -> WorkLane | None:
        if self._desk is None:
            return None
        tracing = TaskTracing(self.telemetry, self._workspace_id, lambda: self._keeper.id)
        beside = Beside(self._lane, self._startup, self._gate, self._coders, tracing)
        return WorkLane(self._desk, self._tasks, self._verifier, beside)

    def _startup_check(
        self, verification: Verification | None, wanted: bool
    ) -> StartupCheck | None:
        """The check to run at startup, if ``wanted``; none without steps to run, nor without
        agents or a workspace to say where to run them."""
        if not wanted or verification is None or not verification.steps:
            return None
        site = self._check_site()
        if site is None:
            return None
        return StartupCheck(verification, site, self._events.put, self._add_startup_fix)

    def _check_site(self) -> CheckSite | None:
        """Where the startup check runs: where the agents work, else in the workspace given."""
        if self._desk is not None:
            return CheckSite(self._desk.agents.tree, self._desk.agents.root)
        return None if self._workspace is None else CheckSite(None, self._workspace)

    async def _add_startup_fix(self, step: VerificationStep, output: str) -> None:
        """Add a draft task to fix ``step``, which failed at startup with ``output``, to the
        session open once no switch is under way."""
        prompt = startup_fix_prompt(step, output)
        task = TaskSpec(prompt=prompt, settings=Settings())
        if self._desk is not None:
            task = role_task(CODER, self._desk.agents.roles[CODER], prompt)
        await self._switcher.between_switches(
            lambda: self._tasks.change(lambda board: board.add_draft(task))
        )

    @property
    def workspace_id(self) -> str:
        """The id of the workspace this host serves."""
        return self._workspace_id

    @property
    def path(self) -> str | None:
        """The path of the workspace this host serves, if it's given one."""
        return self._workspace

    @property
    def display_name(self) -> str:
        """What the workspace this host serves is called when it's told of: its path, or its id
        if it's given none."""
        return self._workspace or self._workspace_id

    @property
    def managed(self) -> bool:
        """Whether the workspace this host serves is a clone orbit made."""
        return self._managed

    @property
    def activity(self) -> Activity:
        """What's in progress in the open session now."""
        return self._activity()

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
        return self._takeover.read_only

    @property
    def owner(self) -> WorkspaceOwner | None:
        """The other orbit that owns the repository while this one is read-only, if any."""
        return self._owner if self._takeover.read_only else None

    @property
    def storage_halted(self) -> bool:
        """Whether a write to the store failed, so the session is halted until resumed."""
        return self._halt.halted

    @property
    def queued_work(self) -> tuple[QueuedWork, ...]:
        """The commands that start work, queued while read-only, oldest first."""
        return self._commands.queued

    @property
    def outbox(self) -> Outbox:
        """Where this host's events are published, each stamped as it is."""
        return self._events

    @property
    def connections(self) -> Connections:
        """The connections among which this host's events are routed."""
        return self._connections

    @property
    def place(self) -> Place:
        """Where a connection attached to this workspace is."""
        return Place(self._events, self._events.stamp, self._connections)

    @property
    def greeting(self) -> HostGreeting:
        """What the handshake of a connection publishes of this host as it first attaches."""
        return self._greeting

    async def entry(self) -> WorkspaceEntry:
        """This workspace's entry in the catalog: its sessions as they are now."""
        return await self._switcher.entry()

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles of this workspace's agents; none without agents."""
        return tuple(self._desk.agents.roles) if self._desk else ()

    async def attach(self, connection: SessionConnection, session: str) -> None:
        """Have ``connection`` view the session ``session`` and publish its snapshot to it;
        refused for a session there's none of."""
        await self._views.attach(connection, session)

    @property
    def closing(self) -> bool:
        """Whether this host is closing, or closed."""
        return self._closing

    async def open_latest(self) -> None:
        """Open the most recently active saved session no other orbit has open in place of the
        new one, if there's one that can be opened, as the user would: one refused, as another
        orbit opened it meanwhile, is passed over for the next. The new one stays open if none
        can be."""
        for summary in await self._keeper.summaries():
            if summary.locked or summary.unreadable is not None:
                continue
            with suppress(Refused):
                await self._switcher.handle(ActivateSession(session=summary.id))
                return

    def check_addressed(self, connection: SessionConnection, session: str | None) -> None:
        """Refuse a command for ``session``, if it names one, unless it's the session
        ``connection`` views."""
        self._views.check_addressed(connection, session)

    async def handle(
        self,
        command: SessionCommand | WorkCommand | History | ResumeStorage | TakeOwnership | Attach,
        command_id: str,
        connection: SessionConnection,
    ) -> None:
        """Handle ``command``, sent as ``command_id`` from ``connection`` once its handshake is
        done: refused once closing, and while the session is halted, but for ``ResumeStorage``,
        or but for those a view lets through while ``connection`` views a session that isn't
        open."""
        self._views.admit(connection, command)
        await self._admitted(command, command_id, connection)

    async def _admitted(
        self,
        command: SessionCommand | WorkCommand | History | ResumeStorage | TakeOwnership | Attach,
        command_id: str,
        connection: SessionConnection,
    ) -> None:
        """Handle ``command``, sent as ``command_id`` from ``connection`` once the handshake is
        done: refused once closing, and while the session is halted, but for ``ResumeStorage``,
        which resumes it."""
        if self._closing:
            raise Refused(INVALID, STOPPING)
        if isinstance(command, ResumeStorage):
            await self._resume_storage()
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
        # The session left is the one the switch left, as others' switches may run meanwhile.
        left = await self._switcher.activate(command)
        if isinstance(left, AlreadyOpen):
            await self._views.attach(connection, self._keeper.id)
        elif left is not None:
            await self._views.activated(left)

    async def _handle(
        self,
        command: RenameSession | DeleteSession | WorkCommand | History | TakeOwnership,
        command_id: str,
    ) -> None:
        if isinstance(command, History):
            await self._events.put(await self._history.page(command))
        elif isinstance(command, RenameSession | DeleteSession):
            await self._change_saved(command)
        elif isinstance(command, TakeOwnership):
            await self._handover.answer(command, command_id)
        else:
            self._switcher.stay()
            await self._commands.handle(command, command_id)

    async def _change_saved(self, command: RenameSession | DeleteSession) -> None:
        """Rename or delete the session ``command`` names, and only once it's done, have the
        connections attached to it name it so, or view the open session in its place."""
        if isinstance(command, RenameSession):
            if await self._switcher.change_saved(command):
                self._views.renamed(command.session_id, command.name)
            return
        await self._views.check_deletable(command.session_id)
        if await self._switcher.change_saved(command):
            await self._views.deleted(command.session_id)

    async def _resume_storage(self) -> None:
        """Lift the halt, if the writes that failed now work, start the run whose wait to start
        again ended while halted, the reviews waiting and the startup check the halt kept from
        starting. A startup check the halt cancelled stays cancelled, as one the user cancelled
        does: it runs once as orbit starts, the conversation has said it was cancelled, and the
        first task's verification runs the same steps. Closing meanwhile, nothing is started."""
        await self._halt.resume()
        if not self._halt.halted and not self._closing:
            await self._turns.lifted()
            if self._lane is not None:
                self._lane.kick()
            self._start_startup_check()

    def _agent_on(self, task_id: str) -> str:
        """The agent working on task ``task_id``; only asked while one is."""
        assert self._desk is not None  # Only agents propose.
        return self._desk.working_on(task_id).id

    def _review_lane(self) -> ReviewLane | None:
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        planner = ReviewPlanner(desk.agents.review_limits)
        hooks = ReviewHooks(
            lambda: self._turns.wake(),
            self._reviews_changed,
            self._gate,
        )
        lane = ReviewLane(desk, self._tasks, planner, hooks)
        desk.suggest = lane.suggest  # Set here, as the lane needs the desk it takes calls from.
        return lane

    async def _reviews_changed(self) -> None:
        """Save the review state, which may have changed, and the repository's super review
        count."""
        assert self._lane is not None  # Only the review lane changes it.
        await self._review_count.save(self._lane.planner.since_super)
        await self._save()

    async def _load_review_count(self) -> None:
        """Have the review planner, if there is one, count on from the repository's saved super
        review count, read once, as the background work starts."""
        if self._lane is not None:
            await self._review_count.count_on(self._lane.planner)

    async def _recount_reviews(self) -> None:
        """Read the repository's saved super review count again, now this orbit owns it: the
        orbit that owned it may have counted on since it was read."""
        if self._lane is not None:
            await self._review_count.recount_on(self._lane.planner)

    async def _save(self) -> None:
        """Save the session, if it's used, as it is now."""
        await self._switcher.save()

    async def _publish_agents(self, event: Event) -> None:
        """Publish ``event``, from the agents; one that ends a turn adds what the turn cost to
        what the session spent, and records it in the background. Publishing never suspends,
        so a turn is not cancelled between its TurnFinished and its own bookkeeping, which saves
        the session after it."""
        if isinstance(event, TurnFinished) and event.usage is not None:
            self._switcher.spent += event.usage
            self._record_usage(event.agent_id, event.usage)
        await self._events.put(event)

    def _record_usage(self, agent_id: str | None, usage: Usage) -> None:
        """Record ``usage``, what a turn of agent ``agent_id`` cost, unless that agent wasn't
        launched."""
        assert self._desk is not None, "Only agents publish turns."
        launch = self._desk.launched(agent_id)
        if launch is not None:
            self._usage.record(self._workspace_id, self._keeper.id, launch, usage)

    def _step_names(self) -> tuple[str, ...]:
        """The names of the steps verifying tasks now; none without verification."""
        return self._verifier.step_names if self._verifier else ()

    async def close(self) -> None:
        """Stop a storage halt under way, the tries of the repository's lock and the loading of
        the super review count, then the work, as :meth:`_stop_work` does: the review lane and
        the startup check, so that nothing wakes the work lane, then the work in progress, which
        stops its agent, and the planner's turn; then stop the retired planners, the planner,
        the agents' host, the recording of turns' usage, the saving of the session, releasing
        its lock, and the writing of its events to the store, then end every connection's
        stream, in that order. Each is stopped
        even if one before it fails or the close is cancelled, as :func:`close_all` does."""
        self._closing = True
        await close_all(self._stops())

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order: nothing that could wake the work lane runs once
        its turn is cancelled, as in :meth:`_stop_work`."""
        stops: list[Callable[[], Awaitable[None]]] = [
            self._halt.close,
            self._takeover.close,
            self._count_loader.close,
        ]
        stops += self._work_stops(self._stop_turns, final=True)
        stops += [self._seat.close_retired, self._seat.close]
        if self._desk is not None:
            stops.append(self._desk.stop)
        stops += [self._usage.close, self._keeper.close, self._events.close, self._end_connections]
        return stops

    async def _end_connections(self) -> None:
        """End every connection's stream once what it has queued is read."""
        self._connections.end()

    async def _stop_turns(self) -> None:
        """Cancel the work lane's and the planner's turns, and wait for them to end; the work
        lane's turns stop for good."""
        await self._turns.close()
        await self._seat.stop_turn()

    async def _halt_turns(self) -> None:
        """Cancel the work lane's and the planner's turns as the session halts, and wait for
        them to end; a run the usage limit stopped still starts again."""
        await self._turns.stop_turn()
        await self._seat.stop_turn()

    async def _stop_work(self) -> None:
        """Cancel the reviews and the startup check, then the work lane's and the planner's
        turns, as the session halts, and wait for them to end; nothing that could wake the work
        lane runs once its turn is cancelled, but a run the usage limit stopped still waits to
        start again."""
        for stop in self._work_stops(self._halt_turns, final=False):
            await stop()

    def _work_stops(
        self, stop_turns: Callable[[], Awaitable[None]], *, final: bool
    ) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`_stop_work` awaits, in order: the review lane, the startup check, then
        ``stop_turns``, which stops the work lane's and the planner's turns. The lane and the
        check are closed for good if ``final``, else stopped, to start again later."""
        parts = (self._lane, self._startup)
        stops: list[Callable[[], Awaitable[None]]] = [
            p.close if final else p.stop for p in parts if p is not None
        ]
        return [*stops, stop_turns]

    def check_state(self) -> None:
        """Publish the open session's state, if that changed."""
        self._states.check()

    def _activity(self) -> Activity:
        """What's in progress now: a task run, a review, a planner turn and the startup check,
        with the tasks the run and the review are on, and where the run waits for a coder, behind
        how many busy."""
        task_id, waiting, busy = self._work_activity()
        return Activity(
            tasks=self._turns.running,
            review=self._lane is not None and self._lane.running,
            planner=self._seat.running,
            startup_check=self._startup is not None and self._startup.running,
            task_id=task_id,
            reviewing=self._lane.reviewing if self._lane else None,
            waiting_for_coder=waiting,
            coders_busy=busy,
        )

    def _work_activity(self) -> tuple[str | None, int | None, int | None]:
        """The task the run is on, where it waits for a coder and behind how many busy; none
        of them without a work lane."""
        work = self._work
        if work is None:
            return None, None, None
        return work.current, work.waiting_for_coder, work.coders_busy

    def start_background(self) -> None:
        """Start loading the super review count, the startup check, if there is one and the
        session isn't halted, and the tries of the repository's lock, unless they were started
        for an earlier connection attaching here. A startup check a halt kept from starting
        starts once the halt lifts."""
        if self._started:
            return
        self._started = True
        self._count_loader.start(self._load_review_count())
        self._takeover.start()
        self._start_startup_check()

    def _start_startup_check(self) -> None:
        """Start the startup check, if there is one, once the background was started, unless
        it was started already or the session is halted."""
        startup = self._startup
        if self._started and startup is not None and not startup.started and not self._halt.halted:
            startup.start()

    def _no_steps(self) -> bool:
        """Whether verification is on with no steps to verify tasks."""
        return self._verifier is not None and not self._verifier.step_names

    async def _leftovers(self) -> list[str]:
        """Notes on the work earlier runs left in the coder's tree; none while read-only, as
        the owner's live work would look left over."""
        tree = self._desk.agents.tree if self._desk and not self._takeover.read_only else None
        return await tree.leftovers() if tree else []

    async def _snapshot(self, last_active: datetime | None = None) -> Shown:
        """The session open here, last active at ``last_active``, as it stands now, with its
        latest events, and why it shows none if they can't be read."""
        transcript = await self._events.transcript(self._keeper.id)
        # Read after the transcript, so the state is as it stands once that's read.
        steps, title = self._steps_shown()
        snapshot = self._views.snapshot(
            session=self._switcher.summary(last_active),
            settings=self._settings.current,
            busy={WORK_LANE: self._turns.running, PLANNER_LANE: self._seat.running},
            tasks=self._board.tasks,
            steps=steps,
            steps_title=title,
            review=self._lane.status if self._lane else None,
            proposals=self._proposals.waiting,
            agents=self._running_agents(),
            transcript=transcript.events,
            viewers=self._states.of(self._keeper.id).viewers,
        )
        return Shown(snapshot, transcript.error)

    def _entry(self, sessions: tuple[SessionSummary, ...]) -> WorkspaceEntry:
        """This workspace's entry in the catalog, holding ``sessions``, the open one's state as
        a connection joining would see it."""
        live = self._states.current(joining=True).values()
        workspace = (self._workspace_id, _workspace_label(self._workspace), self._workspace)
        return workspace_entry(
            workspace,
            sessions,
            read_only=self._takeover.read_only,
            managed=self._managed,
            live=live,
        )

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
