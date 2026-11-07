"""The application's core: one workspace's backend, which the control plane routes the
frontends' commands to.

It takes commands from the frontends, asks the domain what follows from them, drives the
ports to do it, and reports what happened as events.
"""

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime

from orbit.agent.application.closing import close_all
from orbit.agent.application.coder_slots import CoderSeat, CoderSlots
from orbit.agent.application.connection import Connections, Place, SessionConnection
from orbit.agent.application.conversation_seats import ConversationSeats
from orbit.agent.application.desk import AgentDesk, Agents
from orbit.agent.application.handover import Handover, HandoverParts
from orbit.agent.application.handshake import HostGreeting
from orbit.agent.application.history import SessionHistory
from orbit.agent.application.host_views import (
    ActivityParts,
    CatalogEntries,
    EntryParts,
    SessionActivity,
    SessionSnapshots,
    SnapshotParts,
)
from orbit.agent.application.keeper import (
    AlreadyOpen,
    Clock,
    IdMaker,
    NewSessions,
    SessionKeeper,
    local_now,
    new_session_id,
)
from orbit.agent.application.lane_runner import LaneParts, LaneRunner, Lanes, RuntimePauses
from orbit.agent.application.lanes import Background, SessionGate
from orbit.agent.application.log_writer import Overflow
from orbit.agent.application.merge_holds import HoldParts, MergeHolds
from orbit.agent.application.outbox import EventSink, Outbox, Shown
from orbit.agent.application.planner_seat import SeatHooks
from orbit.agent.application.planner_trace import PlannerTracing
from orbit.agent.application.ports import (
    CoderTree,
    Fallback,
    ProposalReview,
    RepositoryLock,
    SessionStore,
    UsageLedger,
)
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.record_moves import MoveHooks, RecordMoves
from orbit.agent.application.reports import ReportCommand, Reports
from orbit.agent.application.review_count import ReviewCount
from orbit.agent.application.review_lane import ReviewHooks, ReviewLane
from orbit.agent.application.review_trace import ReviewTracing
from orbit.agent.application.role_copies import RoleCopies, copy_ttls, saved_copies
from orbit.agent.application.schedules import (
    FiredTimes,
    ScheduleClock,
    ScheduleParts,
    Schedules,
)
from orbit.agent.application.session_state import SessionStates, StateParts
from orbit.agent.application.set_runs import SetRuns, SetSite
from orbit.agent.application.set_trace import SetTracing
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.startup_check import CheckSite, StartupCheck
from orbit.agent.application.startup_trace import StartupTracing
from orbit.agent.application.storage_halt import HaltHooks, StorageHalt
from orbit.agent.application.storage_resume import ResumeParts, StorageResume
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
from orbit.agent.application.tool_call_approvals import ToolCallApprovals
from orbit.agent.application.triggers import TriggerEngine, TriggerHooks
from orbit.agent.application.usage_accounting import AccountParts, UsageAccounting
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
from orbit.agent.domain.catalog import trigger_entries
from orbit.agent.domain.merge_holds import holding_sets
from orbit.agent.domain.ownership import WorkspaceOwner, queued_dropped_note, read_only_note
from orbit.agent.domain.refusals import STOPPING, Refused
from orbit.agent.domain.reviews import ReviewPlanner
from orbit.agent.domain.role_lanes import merging_lane, review_lane, task_lanes
from orbit.agent.domain.roles import Role, disabled_roles, enabled_roles
from orbit.agent.domain.runtimes import ModelAliases
from orbit.agent.domain.storage import StorageSettings
from orbit.agent.domain.triggers import (
    SetOutcome,
    VerificationState,
    can_queue_reviews,
    scheduled_crons,
    verification_state,
)
from orbit.agent.domain.usage_limits import Outages, RuntimeStop
from orbit.agent.domain.verification import VerificationStep
from orbit.protocol import (
    INVALID,
    REVIEW_ROLES,
    TASK_CHECKS,
    ActivateSession,
    Attach,
    DeleteReport,
    DeleteSession,
    Event,
    History,
    ListReports,
    NewSession,
    Notice,
    RenameSession,
    ResumeStorage,
    Settings,
    ShowReport,
    TakeOwnership,
    TriggerEntry,
    UsageChanged,
    WorkspaceEntry,
)
from orbit.tracing import NoTelemetry, Telemetry


def _verifier(
    verification: Verification | None,
    tasks: Tasks,
    events: EventSink,
    agents: Agents | None,
    telemetry: Telemetry,
) -> Verifier | None:
    """What checks ``tasks`` (in the agents' tree, if any), timing the steps agents' calls run
    with ``telemetry``; none without ``verification``."""
    if verification is None:
        return None
    verifier = Verifier(verification, tasks, events.put, agents.tree if agents else None)
    verifier.telemetry = telemetry
    return verifier


def _reviewed(agents: Agents | None, review: ProposalReview) -> Agents | None:
    """``agents``, with proposals going to ``review`` unless they already go somewhere."""
    if agents is None or agents.proposal_review is not None:
        return agents
    return replace(agents, proposal_review=review)


_DEFAULT_STORAGE = StorageSettings()
_NO_TELEMETRY = NoTelemetry()
_UTC_SCHEDULES = ScheduleClock()

UNREGISTERED_WORKSPACE = "unregistered"
"""The id of a host's workspace when it's given none, as one with no store isn't registered."""

NO_REPORTS_KEPT = "This orbit has no store to keep reports in."


async def _unannounced(entry: WorkspaceEntry) -> None:
    """Tell no one of ``entry``, as a host no control plane serves has no one to tell."""


async def _nothing_parked() -> Sequence[str]:
    """No notes, as a host given no fallback files to recover recovers none."""
    return ()


@dataclass(frozen=True)
class _LanePauses:
    """Pauses runtimes through the work lane's ``turns``, made after the lanes that use this."""

    turns: Callable[[], WorkTurns]

    def pause_for(self, stop: RuntimeStop, error: str) -> bool:
        return self.turns().pause_for(stop, error)

    def heard_from(self, runtime: str) -> None:
        self.turns().heard_from(runtime)


@dataclass(frozen=True, kw_only=True)
class HostParts:
    """What a :class:`WorkspaceHost` is built from, each with a default, so a caller gives only
    those it has; :class:`WorkspaceHost` says what each does. They're its ``settings``,
    ``verification``, ``agents``, ``workspace`` and ``workspace_id``, whether to
    ``check_at_startup``, raising ``session_started``, the ``store`` its sessions are saved in,
    the ``clock``, ``new_id``, ``pinned``, ``pick_session``, ``name``, ``startup_notes`` and
    ``storage`` of its sessions, the ``fallback`` and ``recover`` of a halt, the ``owner``,
    ``lock``, ``lock_retry`` and ``lock_declined_retry`` of a takeover, whether it's
    ``managed``, the ``ledger`` its usage is recorded in, the ``telemetry`` it's traced with
    and the ``schedule_clock`` its ``schedule`` triggers tell the time by."""

    settings: Settings | None = None
    verification: Verification | None = None
    agents: Agents | None = None
    workspace: str | None = None
    check_at_startup: bool = False
    workspace_id: str = UNREGISTERED_WORKSPACE
    store: SessionStore | None = None
    clock: Clock = local_now
    new_id: IdMaker = new_session_id
    pinned: Collection[str] = ()
    pick_session: bool = False
    name: str | None = None
    startup_notes: Sequence[str] = ()
    storage: StorageSettings = _DEFAULT_STORAGE
    fallback: Fallback | None = None
    recover: Callable[[], Awaitable[Sequence[str]]] = _nothing_parked
    owner: WorkspaceOwner | None = None
    lock: RepositoryLock | None = None
    lock_retry: float = RETRY_SECONDS
    lock_declined_retry: float = DECLINED_RETRY_SECONDS
    managed: bool = False
    ledger: UsageLedger | None = None
    telemetry: Telemetry = _NO_TELEMETRY
    schedule_clock: ScheduleClock = _UTC_SCHEDULES


_NO_PARTS = HostParts()


class WorkspaceHost:
    """One workspace's backend: takes the commands the control plane routes to it, runs work in
    the background and publishes its events. The collaborators and settings named below, from
    ``settings`` to ``telemetry``, are the fields of its ``parts``.

    The session owns the task board and reports it whole on every change. It starts the lanes
    of the conversational roles, whose turns answer prompts, and a lane for each lane of the
    enabled task roles: the work lane, the one whose roles merge, runs its tasks on the user's
    asking, and the others start theirs by themselves, each on a ``LaneRunner``.
    ``ConversationSeats`` holds a seat for the planner and for each other enabled conversational
    role, each with its own conversation, settings and turns in its role's lane; ``WorkLane``
    holds the rules of a task run: verification and fixes;
    ``WorkTurns`` decides when it runs; ``ReviewLane`` queues the reviews and runs them on the
    runner of the review roles' lane, if that isn't the work lane; ``SessionSwitcher`` switches
    between saved sessions; ``WorkCommands`` handles the other commands; ``SessionHistory``
    answers ``History``; ``SessionActivity``, ``SessionSnapshots`` and ``CatalogEntries``
    assemble what the frontends see of it, and ``UsageAccounting`` charges what its agents'
    turns cost. Its ``Outbox`` stamps its
    events, in envelopes naming the workspace by ``workspace_id``, and its ``Connections`` route
    them among the connections the control plane makes, whose ``Handshake`` greets each with
    the :attr:`greeting` as it first attaches here. Commands from every connection are handled
    as they arrive. Its catalog entry says it's ``managed`` if it's a clone orbit made.

    With ``check_at_startup``, starting its background work raises ``session_started``, as it
    opens the workspace with its session, unless read-only, and never again as it switches
    sessions, its ``verification`` field as the ``verification`` steps, the task checks, say. A
    trigger on it with ``checks = "task-checks"``, such as ``startup-check``, runs those steps
    once in the background, on the base commit, where the agents work, once the session isn't
    halted; the step that fails raises ``startup_check_failed``, whose ``startup-fix`` trigger
    adds a draft task to fix it, unless the coder is disabled. With verification on and no
    steps, ``set-up-verification`` queues the configurator's task first, which runs as ctrl+e's
    does. A trigger with ``checks`` naming one of ``verification``'s sets, on whatever event,
    runs that set in the background, in a snapshot of the base's latest commit, while the agents
    work in a tree; one more run is queued if it fires while the set runs, and a halt cancels the
    runs as it does the startup check. A host without agents fires no triggers.

    Once ``session_started``'s triggers have fired, the enabled ``schedule`` triggers, as they
    stand then, fire on the ``schedule_clock``: once each that came due since it last fired,
    then each time it comes due, until the host closes, the time each last fired kept in
    ``store``, per repository. They raise ``schedule`` as any event is raised, so their tasks
    wait in their lanes as other triggers' do, and a set already running runs once more. A
    read-only host starts them only as it takes the repository over, after the work queued,
    without raising ``session_started``.

    Until verification has a step, while it's on, the session's gate holds back every task but
    the configurator's, and the user's runs are refused, saying why; accepted checks start the
    tasks waiting in the lanes that start by themselves.

    A disabled role is left out of its catalog entries: its agents, the triggers that queue its
    tasks and its lane, unless an enabled role shares it; it queues no reviews, and the work
    asked of it is refused.

    A task whose role works in the worktree, such as a coder's or configurator's, takes one of
    :attr:`coder_slots` as it starts, waiting in line for one while every one is taken, and
    gives it back once it ends; the control plane serving the host shares its slots across its
    workspaces.

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

    When a write to the store fails, the session halts: the agents running, the conversational
    roles' turns, the reviews and the startup check are cancelled, an ``ErrorOccurred`` coded
    ``storage_failed`` explains why, and every command but ``Hello``, a connection's first
    ``Attach`` and ``ResumeStorage`` is refused so. Nothing is written to the store from then
    on: the events not logged go to the ``fallback`` instead. ``ResumeStorage`` retries the
    writes; once they work, the events parked are logged, a ``LogGap`` is logged for each span
    of those lost, and the halt is lifted.

    With an ``owner``, another orbit owns the repository and this one is read-only: its
    ``Catalog`` and ``Snapshot`` say so, a notice naming the owner comes first among the
    startup notes, ``RunTasks``, ``FireTrigger`` and ``ReleaseHold`` are accepted and queued
    rather than run, and no work starts by itself: neither the startup check nor reviews run. It
    notes no leftovers, since only the owner can tell its own running work from what earlier runs
    left. Given the
    repository's ``lock``, ``Takeover`` offers it once it's free, trying it every ``lock_retry``
    seconds and after a decline waiting up to ``lock_declined_retry``, and ``Snapshot`` says
    while the offer is open; ``Handover`` answers ``TakeOwnership``, calling ``owned`` first.
    """

    def __init__(self, parts: HostParts = _NO_PARTS) -> None:
        # What its work will be traced with.
        self.telemetry = parts.telemetry
        self.owned: Callable[[], None] = lambda: None
        # The usage limits fetched last, which the control plane serving this host knows.
        self.latest_usage: Callable[[], UsageChanged | None] = lambda: None
        self.catalog_changed: Callable[[WorkspaceEntry], Awaitable[None]] = _unannounced
        self.usage_recorded: Callable[[], None] = lambda: None
        # The coders' slots its tasks take, shared by the control plane serving this host.
        self.coder_slots = CoderSlots()
        self._takeover = Takeover(
            parts.owner is not None,
            parts.lock,
            lambda event: self._events.put(event),
            RetryTimes(parts.lock_retry, parts.lock_declined_retry),
        )
        settings = parts.settings or Settings()
        self._settings = SessionSettings(settings, parts.pinned, self._model_aliases)
        self._storage = parts.storage
        self._halt = self._storage_halt()
        self._gate = SessionGate(
            lambda: self._halt.halted,
            lambda: self._takeover.read_only,
            self._halt.check,
            self._verification_state,
        )
        overflow = Overflow(parts.fallback, self._halt.log_failed)
        self._connections = Connections(self._connections_changed, lambda: self._keeper.id)
        self._workspace_id = parts.workspace_id
        self._store = parts.store
        self._events = Outbox(lambda: self._keeper.id, parts.store, parts.storage, overflow)
        self._events.workspace = parts.workspace_id
        self._events.telemetry = parts.telemetry
        new_sessions = NewSessions(
            parts.clock, parts.new_id, self._events.forget, self._events.catch_up
        )
        self._keeper = SessionKeeper(parts.store, self._events.put, self._halt, new_sessions)
        self._keeper.rename(parts.name)
        self._review_count = ReviewCount(parts.store, self._halt, self._events.put)
        self._schedules = self._make_schedules(parts)
        self._usage = UsageRecorder(parts.ledger, self._halt, self._events.put, parts.clock)
        self._usage.recorded = lambda: self.usage_recorded()
        self._count_loader = Background("review count", self._events.put)
        self._opening = Background("session start", self._events.put)
        self._tasks = Tasks(TaskBoard(), self._events.put, self._save)
        self._board = self._tasks.board
        self._proposals = FrontendReview(self._events.put, self._agent_on)
        agents = _reviewed(parts.agents, self._proposals)
        self._verifier = _verifier(
            parts.verification, self._tasks, self._events, agents, parts.telemetry
        )
        if self._verifier is not None:  # Accepted checks let the tasks waiting for them run.
            self._verifier.steps_changed = self._kick_lanes
        self._tool_calls = ToolCallApprovals(self._events.put_nowait, self._save)
        self._desk = self._agent_desk(agents, parts)
        self._reports = self._kept_reports(parts.store, parts.clock)
        self._workspace = parts.workspace
        self._managed = parts.managed
        self._recover = parts.recover
        self._owner = parts.owner
        owner_notes = () if parts.owner is None else (read_only_note(parts.owner),)
        self._startup_notes = (*owner_notes, *parts.startup_notes)
        self._closing = False
        self._started = False
        self._opens = parts.check_at_startup and not self._takeover.read_only
        self._check_asked = False  # Whether a trigger asked for the startup check.
        self._copies = self._role_copies(parts.clock)
        self._copy_keeper = Background("copies", self._events.put)
        self._seats = self._conversation_seats()
        self._triggers = self._trigger_engine()
        self._lane = self._review_lane()
        self._runners = self._lane_runners()
        self._lanes = self._session_lanes()
        self._coders = CoderSeat(
            lambda: self.coder_slots, parts.workspace_id, lambda: self._keeper.id
        )
        self._record_moves = self._moves_kept_in(parts.store)
        self._startup = self._startup_check(parts.verification)
        self._holds, self._sets = self._check_sets(parts.verification)
        self._outages = Outages()  # One backoff per runtime, which every lane counts on.
        self._work = self._work_lane()
        self._turns = self._work_turns()
        self._activity = self._session_activity()
        self._switcher = self._session_switcher(parts.pick_session)
        self._resume = self._storage_resume()
        self._commands = self._work_commands()
        self._handover = self._make_handover(parts.check_at_startup)
        self._greeting = self._host_greeting()
        self._history = SessionHistory(self._keeper, self._events)
        self._states = self._session_states()
        self._views = self._session_views()
        self._snapshots = self._session_snapshots()
        self._entries = self._catalog_entries()
        self._accounting = self._usage_accounting()
        self._watch_background()

    def _make_schedules(self, parts: HostParts) -> Schedules:
        """What fires the ``schedule`` triggers on the ``parts``' ``schedule_clock`` once the
        workspace opens, keeping the time each last fired in their ``store``."""
        times = FiredTimes(parts.store, self._halt, self._events.put)
        clock = parts.schedule_clock
        return Schedules(ScheduleParts(times, clock, self._fire_scheduled, self._events.put))

    def _connections_changed(self) -> None:
        """Publish the sessions' states, and tell the seats and the agents' calls held for
        approval how many frontends view the open session, as connections come, go or move."""
        self._states.check()
        viewers = self._connections.viewed()[self._keeper.id]
        self._seats.note_viewers(viewers)
        self._tool_calls.note_viewers(viewers)

    def _agent_desk(self, agents: Agents | None, parts: HostParts) -> AgentDesk | None:
        """The desk launching ``agents`` for the ``parts``' workspace, in the session open,
        publishing as :meth:`_publish_agents` does, tracing with their telemetry and holding the
        agents' calls that need approval among the session's; none without agents."""
        if agents is None:
            return None
        desk = AgentDesk(
            agents, self._publish_agents, parts.telemetry, parts.workspace_id, self._tool_calls
        )
        desk.session = lambda: self._keeper.id
        return desk

    def _conversation_seats(self) -> ConversationSeats:
        """The conversational roles' seats in this session, their turns traced with
        :attr:`telemetry`."""
        tracing = PlannerTracing(self.telemetry, self._workspace_id, lambda: self._keeper.id)
        hooks = SeatHooks(
            self._save,
            self._keeper.use,
            self._step_names,
            self._halt.check,
            tracing,
            self._copies,
            lambda role: self._settings.of(role),
            halted=self._gate.halted,
        )
        return ConversationSeats(self._desk, self._tasks, self._events, hooks)

    def _kept_reports(self, store: SessionStore | None, clock: Clock) -> Reports | None:
        """The reports kept in ``store``, dated by ``clock``, whose tools the desk's agents are
        given; none without a store."""
        if store is None:
            return None
        reports = Reports(store, lambda: self._keeper.id, clock)
        if self._desk is not None:
            self._desk.report_tools = reports.tools
        return reports

    def _role_copies(self, clock: Clock) -> RoleCopies | None:
        """The copies the conversational roles work in, their turns timed by ``clock``, saving
        the session as copies are removed by age; none without agents that make copies."""
        made = self._desk.agents.copies if self._desk else None
        if made is None:
            return None
        copies = RoleCopies(made, clock, self._events.put)
        copies.changed = self._save
        return copies

    async def _keep_copies(self, copies: RoleCopies) -> None:
        """Remove ``copies`` and those of the other saved sessions past their roles' TTL, and
        those left over, then this session's past their TTL every hour."""
        assert self._desk is not None  # Only agents make copies.
        desk = self._desk
        await copies.keep(
            lambda: saved_copies(self._store, self._keeper.id), lambda: copy_ttls(desk.agents.roles)
        )

    def _storage_halt(self) -> StorageHalt:
        """What halts the session as a write to the store fails: it stops the log and the work,
        and retries the writes once resumed."""
        hooks = HaltHooks(
            lambda: self._events.stop_log(self._keeper.id),
            self._stop_work,
            lambda: self._resume.retry(),
        )
        return StorageHalt(lambda event: self._events.put(event), hooks)

    def _storage_resume(self) -> StorageResume:
        """What resumes the session once storage works again: it retries the session record,
        the review count, the usage, the record moves, the schedules' last-fired times and the
        holds on merges, then appends to the event log again."""
        parts = ResumeParts(
            self._keeper,
            self._switcher.contents,
            (
                self._review_count,
                self._usage,
                self._record_moves,
                self._schedules.times,
                *self._kept_holds(),
            ),
            self._events,
            self._turns,
            self._restart_background,
            lambda: self._closing,
        )
        return StorageResume(self._halt, parts)

    def _session_states(self) -> SessionStates:
        """What publishes the open session's state as it changes, its run paused as the work
        lane's turns say."""
        parts = StateParts(
            lambda: self._keeper.id,
            self._activity.now,
            self._connections,
            self._events.put_nowait,
            lambda: self._turns.paused,
            lambda: self._switcher.spend,
            lambda: () if self._holds is None else self._holds.views,
            lambda: () if self._work is None else self._work.waiting_to_merge,
        )
        return SessionStates(parts)

    def _kept_holds(self) -> tuple[MergeHolds, ...]:
        """The holds on merges, as a save resuming retries, if there are any."""
        return () if self._holds is None else (self._holds,)

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

    def _session_activity(self) -> SessionActivity:
        """What reads what's in progress in the open session."""
        parts = ActivityParts(
            self._turns, self._work, self._lane, self._runners, self._seats, self._startup
        )
        return SessionActivity(parts)

    def _session_snapshots(self) -> SessionSnapshots:
        """What makes the open session's snapshot."""
        parts = SnapshotParts(
            lambda: self._keeper.id,
            self._events.transcript,
            self._switcher,
            self._settings,
            self._seats,
            self._board,
            self._proposals,
            self._turns,
            self._lanes,
            self._lane,
            self._startup,
            self._verifier,
            self._desk,
            self._states,
            self._views,
            self._tool_calls,
            self._sets,
        )
        return SessionSnapshots(parts)

    def _catalog_entries(self) -> CatalogEntries:
        """What makes this workspace's entry in the catalog."""
        parts = EntryParts(
            self._workspace_id,
            self._workspace,
            self._managed,
            lambda: self._takeover.read_only,
            self._states,
            lambda: self.agents,
            self._seats,
        )
        return CatalogEntries(parts)

    def _usage_accounting(self) -> UsageAccounting:
        """What publishes the agents' events, charging what their turns cost to the open session
        and publishing its state as that changes."""
        parts = AccountParts(
            self._switcher,
            self._desk,
            self._seats.asker,
            self._usage,
            self._workspace_id,
            lambda: self._keeper.id,
            self._events.put,
            self._states.check,
        )
        return UsageAccounting(parts)

    def _watch_background(self) -> None:
        """Have the open session's state checked whenever the reviews, the other lanes that start
        by themselves or the startup check stop, as nothing is published then: the lanes make it
        running, and the startup check is among the activity it reports."""
        for part in (self._lane, self._startup, *self._runners):
            if part is not None:
                part.on_stopped(self._states.check)

    def _session_switcher(self, picking: bool) -> SessionSwitcher:
        """What switches this session between saved ones, still ``picking`` one if so."""
        parts = SwitchParts(
            self._keeper,
            self._tasks,
            self._seats,
            self._lane,
            self._work,
            self._activity.now,
            self._snapshot,
            lambda sessions: self._entries.entry(sessions),
            lambda entry: self.catalog_changed(entry),
            self._switching,
            self._tool_calls,
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
            self._seats,
            self._turns,
            self._lane,
            self._proposals,
            self._desk,
            self._startup,
            self._save,
            self._reviews_changed,
            self._tool_calls,
            self._gate,
            self._lanes,
            self._sets,
            self._holds,
        )
        return WorkCommands(parts)

    def _make_handover(self, check_at_startup: bool) -> Handover:
        """What answers the offer of the repository, starting the schedules once it's taken if
        ``check_at_startup``, as opening the workspace would have."""
        parts = HandoverParts(
            self._takeover,
            self._commands,
            self._events,
            lambda: self.owned(),
            lambda: self._switcher.publish_catalog(),
            self._recover,
            self._reread_saved,
            self._kick_lanes,
            self._start_schedules if check_at_startup else lambda: None,
        )
        return Handover(parts)

    def _kick_lanes(self) -> None:
        """Start the tasks waiting in the lanes that start by themselves, the reviews among
        them, unless closing."""
        if not self._closing:
            self._lanes.kick()

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
        """What decides when the work lane runs, publishing the open session's state as its run's
        pause changes."""
        parts = WorkParts(
            self._work,
            self._lanes,
            self._tasks,
            self._verifier,
            self._desk,
            self._events.put_nowait,
            self._gate,
            lambda: self.latest_usage(),
            lambda: self._states.check(),
            self._lanes.work,
            self._outages,
            self._triggers,
        )
        return WorkTurns(parts)

    def _moves_kept_in(self, store: SessionStore | None) -> RecordMoves:
        """What moves the records tasks name whose numbers others took, keeping each move in
        ``store``; records keep their numbers without agents that can list files."""
        names = self._desk.agents.file_names if self._desk else None
        hooks = MoveHooks(names, self._events.put, self._seats.tell)
        return RecordMoves(self._tasks, store, self._halt, hooks)

    def _work_lane(self) -> WorkLane | None:
        if self._desk is None:
            return None
        desk = self._desk  # Its agents' roles may be read again, so asked as each task runs.
        tracing = TaskTracing(
            self.telemetry,
            self._workspace_id,
            lambda: self._keeper.id,
            lambda role: desk.agents.runtime(role),
        )
        beside = Beside(
            self._lane,
            self._startup,
            self._gate,
            self._coders,
            tracing,
            self._record_moves,
            self._lanes.elsewhere,
            disabled_roles(self._roles()),
            self._outages,
            self._triggers,
            self._sets,
            self._holds,
            self._save,
        )
        return WorkLane(self._desk, self._tasks, self._verifier, beside)

    def _startup_check(self, verification: Verification | None) -> StartupCheck | None:
        """The check a trigger with ``checks = "task-checks"`` runs as the workspace opens, if it
        raises ``session_started``, where the agents work, traced with this host's telemetry;
        none without steps to run, nor without agents, whose triggers would run it."""
        desk = self._desk
        if not self._opens or verification is None or not verification.steps or desk is None:
            return None
        site = CheckSite(desk.agents.tree, desk.agents.root)
        check = StartupCheck(verification, site, self._events.put, self._startup_failed)
        check.tracing = StartupTracing(self.telemetry, self._workspace_id)
        return check

    def _check_sets(
        self, verification: Verification | None
    ) -> tuple[MergeHolds | None, SetRuns | None]:
        """The holds on merges of ``verification``'s named sets and their runs, as
        :meth:`_merge_holds` and :meth:`_set_runs` say; neither while verification is off, nor
        without agents in a tree, whose triggers run the sets in its snapshots, leaving the
        coder's tree alone, and whose sets' runs hold merges."""
        tree = self._desk.agents.tree if self._desk else None
        if verification is None or tree is None:
            return None, None
        holds = self._merge_holds(verification, tree)
        return holds, self._set_runs(verification, tree, holds)

    def _set_runs(self, verification: Verification, tree: CoderTree, holds: MergeHolds) -> SetRuns:
        """The runs of the named sets of ``verification``, and of those accepted checks add, that
        triggers naming them ask for, in snapshots of the base of ``tree``, traced with this
        host's telemetry, the runs of those that hold merges holding them while they fail, as
        ``holds`` keeps them, and each run that ends raising ``set_finished``."""
        site = SetSite(
            tree,
            verification.run_step,
            self._events.put,
            self._gate.halted,
            holds.heard,
            self._set_finished,
        )
        return SetRuns(verification.sets, site, SetTracing(self.telemetry, self._workspace_id))

    def _merge_holds(self, verification: Verification, tree: CoderTree) -> MergeHolds:
        """The holds on merges of the named sets of ``verification`` that hold them, and of those
        accepted checks have hold them, kept in the store, each queuing as many fixes as
        verification's fallback fixes, a fix listing the commits of ``tree``; the review lane's
        merge passes filing blocking tasks hold them too."""
        desk = self._desk
        assert desk is not None  # Only agents work in a tree.
        parts = HoldParts(
            self._tasks,
            lambda: desk.agents.roles,
            tree,
            self._events.put,
            self._store,
            self._halt,
            lambda role: self._lanes.kick_for(role),
            self._rerun_set,
        )
        holds = MergeHolds(holding_sets(verification.sets), verification.fallback_fixes, parts)
        holds.changed = lambda: self._states.check()
        holds.released = self._release_waiting
        if self._lane is not None:
            self._lane.holds = holds  # Its merge passes' blocking tasks hold merges.
        return holds

    def _release_waiting(self) -> None:
        """Land the work waiting for the holds on merges, as the last lifts or a halt does."""
        if self._work is not None:
            self._work.release()

    def _rerun_set(self, name: str, trigger: str) -> None:
        """Run the set ``name`` again, as the trigger named ``trigger`` fired, as a fix for it
        landed."""
        assert self._sets is not None  # Sets that hold merges run here, or none would hold.
        self._sets.run(name, trigger)

    async def _open_session(self) -> None:
        """Raise ``session_started`` in the session open once no switch is under way, as the
        workspace opens with it, verifying tasks with the steps configured now, none while
        verification is off, with the named sets configured, and run the tasks its triggers
        queued, and a configurator's waiting while verification is missing, queued then or saved
        with the session, as ctrl+e would, then start the schedules of the enabled ``schedule``
        triggers; without agents, there are no triggers to fire."""
        engine = self._triggers
        if engine is None:
            return
        verification = self.verification
        steps = None if verification is None else verification.steps
        sets = () if verification is None else verification.sets
        queued = await self._switcher.between_switches(lambda: engine.started(steps, sets))
        engine.run(queued)
        await self._turns.set_up()
        self._start_schedules()

    def _start_schedules(self) -> None:
        """Start firing the enabled ``schedule`` triggers, as they stand now, first those that
        came due while closed, as the workspace opens or this orbit takes the repository over;
        not while closing, nor once started, nor without agents, whose triggers they'd be."""
        desk = self._desk
        if self._closing or desk is None:
            return
        self._schedules.start(scheduled_crons(desk.agents.triggers))

    async def _fire_scheduled(self, names: tuple[str, ...]) -> None:
        """Raise ``schedule`` for the triggers ``names``, come due, in the session open once no
        switch is under way, and run the tasks they queued."""
        engine = self._triggers
        assert engine is not None  # Only agents' triggers are scheduled.
        queued = await self._switcher.between_switches(lambda: engine.scheduled(names))
        engine.run(queued)

    async def _startup_failed(self, step: VerificationStep, output: str) -> bool:
        """Raise ``startup_check_failed`` for ``step``, which failed at startup with ``output``,
        in the session open once no switch is under way, and run the tasks its triggers queued;
        returns whether they queued any."""
        engine = self._triggers
        assert engine is not None  # Only agents' triggers run the startup check.
        queued = await self._switcher.between_switches(lambda: engine.check_failed(step, output))
        engine.run(queued)
        return bool(queued)

    async def _set_finished(self, outcome: SetOutcome) -> bool:
        """Raise ``set_finished`` for ``outcome``, a named set's run ending, in the session open
        once no switch is under way, through the review lane, which books the finding roles'
        tasks into a review of the set's report, if reviews run, and run the tasks its triggers
        queued; returns whether they queued any, a task that carries its output."""
        engine = self._triggers
        assert engine is not None  # Only agents' triggers run the sets.
        lane = self._lane
        if lane is not None:
            booked = await self._switcher.between_switches(lambda: lane.set_finished(outcome))
            return bool(booked)
        queued = await self._switcher.between_switches(lambda: engine.set_finished(outcome))
        engine.run(queued)
        return bool(queued)

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
        return self._activity.now()

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

    def _model_aliases(self, role: str) -> ModelAliases | None:
        """The models role ``role``'s agents may be given, those its runtime takes; none
        without agents, or if they aren't known."""
        return self._desk.agents.model_aliases(role) if self._desk else None

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
        """This workspace's entry in the catalog: its sessions as they are now, and its agents'
        enabled roles."""
        return await self._switcher.entry()

    @property
    def pending_tasks(self) -> int:
        """How many of the open session's tasks are pending."""
        return self._board.pending

    @property
    def log_size(self) -> int:
        """The bytes the logs of this workspace's sessions hold in the store, as it last knew
        them; 0 without a store."""
        return 0 if self._store is None else self._store.log_size()

    @property
    def roles(self) -> dict[str, Role]:
        """The enabled roles of this workspace's agents, by name; none without agents."""
        return enabled_roles(self._roles())

    @property
    def personal(self) -> frozenset[str]:
        """The names of this workspace's roles defined whole in the user config, its personal
        roles; none without agents."""
        return self._desk.agents.personal if self._desk else frozenset()

    @property
    def usage_unknown(self) -> frozenset[str]:
        """The names of this workspace's roles whose agents' usage is unknown, as their runtime
        doesn't report it; none without agents."""
        return self._desk.agents.usage_unknown if self._desk else frozenset()

    @property
    def triggers(self) -> tuple[TriggerEntry, ...]:
        """The catalog's entries for this workspace's triggers: those enabled whose roles aren't
        all disabled, with their tabs, then the built-in triggers that can be fired here but
        aren't among them."""
        triggers = self._desk.agents.triggers if self._desk else {}
        return trigger_entries(triggers, self._roles())

    @property
    def lanes(self) -> tuple[str, ...]:
        """The names of this workspace's lanes: the work lane, the conversational roles', a
        disabled planner's only while another role answers there, and those that start by
        themselves, which only enabled roles have."""
        return self._lanes.names

    @property
    def conversational(self) -> tuple[str, ...]:
        """The roles the user can message here: the planner and the other enabled conversational
        roles."""
        return self._seats.names

    def _roles(self) -> Mapping[str, Role]:
        """The roles of this workspace's agents, whether enabled or not; none without
        agents."""
        return self._desk.agents.roles if self._desk else {}

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
        command: SessionCommand
        | WorkCommand
        | History
        | ResumeStorage
        | TakeOwnership
        | Attach
        | ReportCommand,
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
        command: SessionCommand
        | WorkCommand
        | History
        | ResumeStorage
        | TakeOwnership
        | Attach
        | ReportCommand,
        command_id: str,
        connection: SessionConnection,
    ) -> None:
        """Handle ``command``, sent as ``command_id`` from ``connection`` once the handshake is
        done: refused once closing, and while the session is halted, but for ``ResumeStorage``,
        which resumes it."""
        if self._closing:
            raise Refused(INVALID, STOPPING)
        if isinstance(command, ResumeStorage):
            await self._resume.resume()
            return
        self._halt.check()
        if isinstance(command, Attach):
            await self._views.attach(connection, command.session)
            return
        if isinstance(command, ActivateSession | NewSession):
            await self._activate(command, connection)
            return
        await self._handle(command, command_id, connection)

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
        command: RenameSession
        | DeleteSession
        | WorkCommand
        | History
        | TakeOwnership
        | ReportCommand,
        command_id: str,
        connection: SessionConnection,
    ) -> None:
        if isinstance(command, History):
            await self._events.put(await self._history.page(command))
        elif isinstance(command, ListReports | ShowReport | DeleteReport):
            await self._answer_reports(command, connection)
        elif isinstance(command, RenameSession | DeleteSession):
            await self._change_saved(command)
        elif isinstance(command, TakeOwnership):
            await self._handover.answer(command, command_id)
        else:
            self._switcher.stay()
            await self._commands.handle(command, command_id)

    async def _answer_reports(self, command: ReportCommand, connection: SessionConnection) -> None:
        """Carry out ``command``, sending ``connection`` alone the list or the report it asks
        for; refused without a store to keep reports in."""
        if self._reports is None:
            raise Refused(INVALID, NO_REPORTS_KEPT)
        await self._reports.answer(command, connection.publish_here)

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

    def _restart_background(self) -> None:
        """Start the tasks waiting in the lanes that start by themselves, the reviews among them,
        the startup check and the sets' runs triggers asked for that the halt kept from
        starting, and the conversational roles' turns waiting, such as those hearing of the
        consultations the halt cancelled, and times the consultations and the agents' calls held
        for approval to expire again, as the halt lifts. A startup check the halt cancelled stays
        cancelled, as one the user cancelled does: it runs once as orbit starts, the conversation
        has said it was cancelled, and the first task's verification runs the same steps; so do
        the sets' runs the halt cancelled, until their triggers fire again."""
        self._lanes.kick()
        self._start_startup_check()
        if self._sets is not None:
            self._sets.restart()
        self._release_waiting()
        self._seats.start_waiting()
        self._seats.restart_expiry()
        self._tool_calls.unpause()

    def _agent_on(self, task_id: str) -> str:
        """The agent working on task ``task_id``; only asked while one is."""
        assert self._desk is not None  # Only agents propose.
        return self._desk.working_on(task_id).id

    def _trigger_engine(self) -> TriggerEngine | None:
        """What fires the agents' triggers on events, queuing their tasks on the session's board
        and running each in its role's lane if that starts by itself, or running the startup
        check for those with ``checks = "task-checks"`` and the set named for those naming one;
        none without agents."""
        if self._desk is None:
            return None
        hooks = TriggerHooks(lambda role: self._lanes.kick_for(role), self._run_check, self._gate)
        return TriggerEngine(self._desk, self._tasks, hooks)

    def _review_lane(self) -> ReviewLane | None:
        """The review lane, if reviews are on and run beside the work: in a tree, with a review
        role enabled and the enabled ones in a lane of their own; none otherwise. Reviews of
        commits are queued only while a trigger ``review-rounds`` guards can fire, as the
        agents' triggers stand."""
        desk = self._desk
        if desk is None or not desk.agents.review or desk.agents.tree is None:
            return None
        roles = desk.agents.roles
        reviewers = REVIEW_ROLES - disabled_roles(roles)
        if not reviewers or review_lane(roles) == merging_lane(roles):
            return None
        enabled = enabled_roles(roles)
        planner = ReviewPlanner(
            desk.agents.review_limits, lambda: can_queue_reviews(desk.agents.triggers, enabled)
        )
        hooks = ReviewHooks(
            lambda: self._turns.wake(),
            self._reviews_changed,
            self._gate,
            ReviewTracing(self.telemetry, self._workspace_id, lambda: self._keeper.id),
            self._paused,
            self._events.put_nowait,
            self._pauses,
            self._triggers,
        )
        lane = ReviewLane(desk, self._tasks, planner, hooks)
        desk.suggest = lane.suggest  # Set here, as the lane needs the desk it takes calls from.
        desk.report_finding = lane.report
        desk.dismiss_finding = lane.dismiss
        desk.completion_refusal = lane.completion_refusal
        return lane

    def _lane_runners(self) -> tuple[LaneRunner, ...]:
        """The runners of the lanes that start by themselves but the review lane: every lane of
        the enabled task roles but the one whose roles merge; none without agents."""
        desk = self._desk
        if desk is None:
            return ()
        roles = desk.agents.roles
        taken = {merging_lane(roles), *((self._lane.runner.lane,) if self._lane else ())}
        post = self._events.put_nowait
        parts = LaneParts(desk, self._tasks, self._gate, self._paused, post, self._pauses)
        return tuple(
            LaneRunner(lane, names, parts)
            for lane, names in task_lanes(roles).items()
            if lane not in taken
        )

    def _session_lanes(self) -> Lanes:
        """The session's lanes: the one whose roles merge, which runs on the user's asking, the
        conversational roles', as they stand each time they're named, and those that start by
        themselves, the review lane among them."""
        review = (self._lane.runner,) if self._lane else ()
        runners = (*review, *self._runners)
        return Lanes(merging_lane(self._roles()), runners, lambda: self._seats.lanes)

    @property
    def _pauses(self) -> RuntimePauses:
        """What pauses the runtime of a task in a lane that starts by itself whose agent hit
        its limit or an outage: the work lane's turns, which say each pause."""
        return _LanePauses(lambda: self._turns)

    def _paused(self) -> str | None:
        """The runtime paused, on which no lane that starts by itself starts a task; none while
        none is."""
        paused = self._turns.paused
        return None if paused is None else paused.runtime

    async def _reviews_changed(self) -> None:
        """Save the review state, which may have changed, and the repository's super review
        count."""
        assert self._lane is not None  # Only the review lane changes it.
        await self._review_count.save(self._lane.planner.super_count)
        await self._save()

    async def _load_saved(self) -> None:
        """Have the review planner count on from the repository's saved super review count, and
        the holds on merges take on those kept, as the background work starts."""
        await self._load_review_count()
        if self._holds is not None:
            await self._holds.restore()

    async def _load_review_count(self) -> None:
        """Have the review planner, if there is one, count on from the repository's saved super
        review count, read once, as the background work starts."""
        if self._lane is not None:
            await self._review_count.count_on(self._lane.planner)

    async def _reread_saved(self) -> None:
        """Read the repository's saved super review count and the holds on merges kept again,
        now this orbit owns it: the orbit that owned it may have counted on, or started or
        lifted holds, since they were read."""
        if self._lane is not None:
            await self._review_count.recount_on(self._lane.planner)
        if self._holds is not None:
            await self._holds.restore()

    async def _save(self) -> None:
        """Save the session, if it's used, as it is now."""
        await self._switcher.save()

    async def _publish_agents(self, event: Event) -> None:
        """Publish ``event``, from the agents, charging what a turn that ends cost to the open
        session, as :class:`UsageAccounting` does."""
        await self._accounting.publish(event)

    def _step_names(self) -> tuple[str, ...]:
        """The names of the steps verifying tasks now; none without verification."""
        return self._verifier.step_names if self._verifier else ()

    async def close(self) -> None:
        """Stop a storage halt under way, the tries of the repository's lock, the loading of
        the super review count, the raising of ``session_started``, the schedules and the
        keeping of the copies, then decline the agents' calls held for approval, then the work,
        as :meth:`_stop_work` does: the review lane, the other lanes that start by themselves,
        the startup check and the sets' runs, so that nothing wakes the work lane, then the work
        in progress, which stops its agent, and the conversational roles' turns; then stop their
        retired conversations, their conversations, the agents' host, the recording of turns'
        usage, the saving of the session, releasing its lock, and the writing of its events to
        the store, then end every connection's stream, in that order.
        Each is stopped even if one before it fails or the close is cancelled, as
        :func:`close_all` does."""
        self._closing = True
        await close_all(self._stops())

    def _stops(self) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`close` awaits, in order: nothing that could wake the work lane runs once
        its turn is cancelled, as in :meth:`_stop_work`."""
        stops: list[Callable[[], Awaitable[None]]] = [
            self._halt.close,
            self._takeover.close,
            self._count_loader.close,
            self._opening.close,
            self._schedules.close,
            self._copy_keeper.close,
        ]
        stops.append(self._decline_tool_calls)
        stops += self._work_stops(self._stop_turns, final=True)
        stops += [self._seats.close_retired, self._seats.close]
        if self._desk is not None:
            stops.append(self._desk.stop)
        stops += [self._usage.close, self._keeper.close, self._events.close, self._end_connections]
        return stops

    async def _decline_tool_calls(self) -> None:
        """Decline the agents' calls held for approval, as orbit closes."""
        self._tool_calls.decline_all()

    async def _end_connections(self) -> None:
        """End every connection's stream once what it has queued is read."""
        self._connections.end()

    async def _stop_turns(self) -> None:
        """Cancel the work lane's and the conversational roles' turns, and wait for them to end;
        the work lane's turns stop for good."""
        await self._turns.close()
        await self._seats.stop_turns(final=True)

    async def _halt_turns(self) -> None:
        """Cancel the work lane's and the conversational roles' turns as the session halts, and
        wait for them to end; a run the usage limit stopped still starts again. The agents' calls
        held for approval stop expiring until the halt lifts."""
        self._tool_calls.pause()
        await self._turns.stop_turn()
        await self._seats.stop_turns(final=False)

    async def _stop_work(self) -> None:
        """Cancel the reviews, the other lanes that start by themselves, the startup check and the
        sets' runs, then the work lane's and the conversational roles' turns, as the session
        halts, and wait for them to end, each even if stopping one before it fails, as
        :func:`close_all` does; nothing that could wake the work lane runs once its turn is
        cancelled, but a run the usage limit stopped still waits to start again."""
        await close_all(self._work_stops(self._halt_turns, final=False))

    def _work_stops(
        self, stop_turns: Callable[[], Awaitable[None]], *, final: bool
    ) -> list[Callable[[], Awaitable[None]]]:
        """What :meth:`_stop_work` awaits, in order: the review lane, the other lanes that start
        by themselves, the startup check, the sets' runs, then ``stop_turns``, which stops the
        work lane's and the conversational roles' turns. The lanes and the checks are closed for
        good if ``final``, else stopped, to start again later."""
        parts = (self._lane, *self._runners, self._startup, self._sets)
        stops: list[Callable[[], Awaitable[None]]] = [
            p.close if final else p.stop for p in parts if p is not None
        ]
        landing = [] if self._work is None else [self._work.stop_releasing]
        return [*stops, *landing, stop_turns]

    def check_state(self) -> None:
        """Publish the open session's state, if that changed."""
        self._states.check()

    def start_background(self) -> None:
        """Start loading the super review count, raising ``session_started``, if it raises it,
        the tries of the repository's lock, and the keeping of the copies, unless read-only, as
        the owner's copies would look left over, unless they were started for an earlier
        connection attaching here. A startup check a halt kept from starting starts once the
        halt lifts."""
        if self._started:
            return
        self._started = True
        self._count_loader.start(self._load_saved())
        if self._opens:
            self._opening.start(self._open_session())
        self._takeover.start()
        if self._copies is not None and not self._takeover.read_only:
            self._copy_keeper.start(self._keep_copies(self._copies))

    def _run_check(self, checks: str, trigger: str) -> None:
        """Run the checks a trigger's ``checks`` names, as the trigger named ``trigger`` fired,
        now, unless the session is halted, else once the halt lifts: the startup check, for
        ``"task-checks"``, or else the set it names, if sets run here."""
        if checks != TASK_CHECKS:
            if self._sets is not None:
                self._sets.run(checks, trigger)
            return
        self._check_asked = True
        self._start_startup_check()

    def _start_startup_check(self) -> None:
        """Start the startup check, if there is one, once a trigger asked for it, unless it was
        started already or the session is halted."""
        startup = self._startup
        if startup is None or not self._check_asked:
            return
        if not startup.started and not self._halt.halted:
            startup.start()

    def _no_steps(self) -> bool:
        """Whether verification is on with no steps to verify tasks."""
        return self._verification_state() == "missing"

    def _verification_state(self) -> VerificationState:
        """What verification is now, as ``session_started`` says it: configured with at least
        one step, missing while it's on with none, until accepted checks set some, or off."""
        verifier = self._verifier
        return verification_state(None if verifier is None else verifier.verification.steps)

    async def _leftovers(self) -> list[str]:
        """Notes on the work earlier runs left in the coder's tree; none while read-only, as
        the owner's live work would look left over."""
        tree = self._desk.agents.tree if self._desk and not self._takeover.read_only else None
        return await tree.leftovers() if tree else []

    async def _snapshot(self, last_active: datetime | None = None) -> Shown:
        """The session open here, last active at ``last_active``, as it stands now, with its
        latest events, and why it shows none if they can't be read."""
        return await self._snapshots.snapshot(last_active)
