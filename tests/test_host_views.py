"""What the frontends see of a workspace's open session, assembled from the parts holding it:
what's in progress there, its snapshot and the workspace's entry in the catalog."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Unpack

from orbit.agent.application.host_views import (
    ActivityParts,
    CatalogEntries,
    EntryParts,
    SessionActivity,
    SessionSnapshots,
    SnapshotParts,
)
from orbit.agent.application.lane_runner import Lanes
from orbit.agent.application.outbox import Shown, Transcript
from orbit.agent.application.proposals import FrontendReview
from orbit.agent.application.settings import SessionSettings
from orbit.agent.application.views import SessionParts
from orbit.agent.domain.activity import Activity
from orbit.agent.domain.board import TaskBoard
from orbit.agent.domain.catalog import NO_WORKSPACE_LABEL, agent_entries
from orbit.agent.domain.role_spend import RoleSpend
from orbit.agent.domain.roles import Role
from orbit.agent.domain.skills import SkillMapping
from orbit.agent.domain.verification import StepProgress
from orbit.protocol import (
    BUGS_REVIEWER,
    CODER,
    EXPERIMENTER,
    INTERNAL,
    PLANNER,
    PLANNER_LANE,
    WORK_LANE,
    ApprovalRequested,
    BusyLane,
    ErrorOccurred,
    Event,
    EventEnvelope,
    Notice,
    ReviewStatus,
    RunningAgent,
    SessionStateChanged,
    SessionSummary,
    Settings,
    Snapshot,
    StepState,
    TaskSpec,
    ToolCallHeld,
    Usage,
    VerificationChanged,
    WorkspaceEntry,
    with_state,
)
from tests.agent_fakes import ROLES

OPEN = "session-1"
WORKSPACE = "workspace-1"
LAST_ACTIVE = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
LINT = StepState(name="lint")
TESTS = StepState(name="tests")
STARTUP_AGENT = RunningAgent(agent_id="startup", role="startup", task_id="startup")
CODER_AGENT = RunningAgent(agent_id="agent-1", role=CODER, task_id="1")
APPROVAL = ApprovalRequested(
    consultation=1, asker=PLANNER, consultant=EXPERIMENTER, title="Try it.", asked=6
)
CALL = ToolCallHeld(
    call=1,
    agent_id="agent-1",
    role="coder",
    server="github",
    tool="github__create_issue",
    arguments={"title": "Flaky test"},
)
OPEN_STATE = SessionStateChanged(session=OPEN, active=True, state="idle", viewers=2)


@dataclass
class FakeTurns:
    running: bool = False


@dataclass
class FakeWork:
    current: str | None = None
    waiting_for_coder: int | None = None
    coders_busy: int | None = None


@dataclass
class FakeReviews:
    running: bool = False
    reviewing: tuple[str, ...] | None = None
    task: str | None = None
    status: ReviewStatus = field(default_factory=lambda: ReviewStatus(paused=False, open_batches=0))


@dataclass
class FakeRunner:
    running: bool = False
    current: str | None = None


@dataclass(frozen=True)
class FakeHeldCalls:
    """The agents' calls held for approval, ``waiting``."""

    waiting: tuple[ToolCallHeld, ...] = ()


@dataclass
class FakeSeats:
    names: tuple[str, ...] = (PLANNER,)
    answering: tuple[str, ...] = ()
    approvals: tuple[ApprovalRequested, ...] = ()
    lanes_busy: dict[BusyLane, bool] = field(default_factory=dict)

    def busy(self) -> dict[BusyLane, bool]:
        return self.lanes_busy


@dataclass
class FakeStartup:
    running: bool = False
    panel: VerificationChanged | None = None
    agent: RunningAgent | None = None


@dataclass
class FakeVerifier:
    progress: StepProgress = field(default_factory=StepProgress)


@dataclass
class FakeDesk:
    running: tuple[RunningAgent, ...] = ()


@dataclass
class FakeSwitcher:
    """What the open session is and spent, and when each summary asked for was last active."""

    spend: RoleSpend = field(default_factory=RoleSpend)
    asked: list[datetime | None] = field(default_factory=list)

    def summary(self, last_active: datetime | None) -> SessionSummary:
        self.asked.append(last_active)
        return SessionSummary(id=OPEN, name="open", last_active=last_active)


@dataclass
class FakeStates:
    """The sessions' states, by id, and whether each time they were all asked for counted a
    connection joining."""

    states: dict[str, SessionStateChanged] = field(default_factory=lambda: {OPEN: OPEN_STATE})
    joining: list[bool] = field(default_factory=list)

    def current(self, *, joining: bool = False) -> dict[str, SessionStateChanged]:
        self.joining.append(joining)
        return self.states

    def of(self, session_id: str) -> SessionStateChanged:
        return self.states[session_id]


class FakeViews:
    def snapshot(self, **session: Unpack[SessionParts]) -> Snapshot:
        return Snapshot(model_choices=(), **session)


@dataclass
class FakeRoster:
    roles: Mapping[str, Role] = field(default_factory=lambda: ROLES)
    personal: frozenset[str] = frozenset()
    usage_unknown: frozenset[str] = frozenset()
    skill_mappings: tuple[SkillMapping, ...] = ()


async def unpublished(event: Event) -> None:
    """Publish nothing, as no proposal is shown here."""


@dataclass
class Held:
    """The parts of a workspace and its open session, as what's shown of them reads them;
    nothing runs, and nothing is known, by default."""

    turns: FakeTurns = field(default_factory=FakeTurns)
    work: FakeWork | None = None
    lane: FakeReviews | None = None
    runners: tuple[FakeRunner, ...] = ()
    seats: FakeSeats = field(default_factory=FakeSeats)
    startup: FakeStartup | None = None
    verifier: FakeVerifier | None = None
    desk: FakeDesk | None = None
    switcher: FakeSwitcher = field(default_factory=FakeSwitcher)
    states: FakeStates = field(default_factory=FakeStates)
    board: TaskBoard = field(default_factory=TaskBoard)
    settings: SessionSettings = field(default_factory=lambda: SessionSettings(Settings()))
    transcript: Transcript = field(default_factory=Transcript)
    read: list[str] = field(default_factory=list)
    while_read: Callable[[], None] = lambda: None
    workspace: str | None = None
    read_only: bool = False
    managed: bool = False
    roster: FakeRoster | None = None
    tool_calls: FakeHeldCalls = field(default_factory=lambda: FakeHeldCalls())

    def activity(self) -> Activity:
        """What's in progress now."""
        parts = ActivityParts(
            self.turns, self.work, self.lane, self.runners, self.seats, self.startup
        )
        return SessionActivity(parts).now()

    async def read_transcript(self, session_id: str) -> Transcript:
        self.read.append(session_id)
        self.while_read()
        return self.transcript

    async def shown(self) -> Shown:
        """The open session's snapshot, last active at :data:`LAST_ACTIVE`."""
        return await SessionSnapshots(self.snapshot_parts()).snapshot(LAST_ACTIVE)

    async def snapshot(self) -> Snapshot:
        return (await self.shown()).snapshot

    def snapshot_parts(self) -> SnapshotParts:
        return SnapshotParts(
            lambda: OPEN,
            self.read_transcript,
            self.switcher,
            self.settings,
            self.seats,
            self.board,
            FrontendReview(unpublished, lambda task_id: "agent-1"),
            self.turns,
            Lanes(),
            self.lane,
            self.startup,
            self.verifier,
            self.desk,
            self.states,
            FakeViews(),
            self.tool_calls,
        )

    def entry(self, *sessions: SessionSummary) -> WorkspaceEntry:
        """The workspace's entry in the catalog, holding ``sessions``."""
        parts = EntryParts(
            WORKSPACE,
            self.workspace,
            self.managed,
            lambda: self.read_only,
            self.states,
            lambda: self.roster,
            self.seats,
        )
        return CatalogEntries(parts).entry(sessions)


def entry_of(**fields: object) -> WorkspaceEntry:
    """The entry of the workspace :data:`WORKSPACE` with no directory, sessions or agents, but
    for ``fields``."""
    empty = WorkspaceEntry(id=WORKSPACE, label=NO_WORKSPACE_LABEL, sessions=())
    return empty.model_copy(update=fields)


def test_with_nothing_running_the_session_is_idle() -> None:
    activity = Held().activity()
    assert (activity, activity.running) == (Activity(), False)


def test_a_task_run_shows_its_task_and_where_it_waits_for_a_coder() -> None:
    work = FakeWork(current="3", waiting_for_coder=2, coders_busy=4)
    activity = Held(turns=FakeTurns(running=True), work=work).activity()
    assert activity == Activity(tasks=True, task_id="3", waiting_for_coder=2, coders_busy=4)
    assert activity.running


def test_a_review_shows_the_tasks_it_reviews() -> None:
    activity = Held(lane=FakeReviews(running=True, reviewing=("1", "2"))).activity()
    assert activity == Activity(review=True, reviewing=("1", "2"))


def test_the_lane_tasks_are_the_review_lanes_then_those_of_the_lanes_running() -> None:
    runners = (FakeRunner(running=True, current="5"), FakeRunner(current="6"), FakeRunner(True))
    activity = Held(lane=FakeReviews(task="4"), runners=runners).activity()
    assert activity == Activity(lane_tasks=("4", "5"))


def test_the_roles_answering_and_the_startup_check_running_show() -> None:
    held = Held(seats=FakeSeats(answering=(PLANNER,)), startup=FakeStartup(running=True))
    assert held.activity() == Activity(answering=(PLANNER,), startup_check=True)


async def test_the_startup_checks_steps_show_under_its_title_while_it_has_the_panel() -> None:
    panel = VerificationChanged(steps=(LINT,), title="Startup check")
    held = Held(startup=FakeStartup(panel=panel), verifier=FakeVerifier(StepProgress((TESTS,))))
    snapshot = await held.snapshot()
    assert (snapshot.steps, snapshot.steps_title) == ((LINT,), "Startup check")


async def test_the_steps_verifying_tasks_show_untitled_otherwise() -> None:
    held = Held(startup=FakeStartup(), verifier=FakeVerifier(StepProgress((TESTS,))))
    snapshot = await held.snapshot()
    assert (snapshot.steps, snapshot.steps_title) == ((TESTS,), "")


async def test_no_steps_show_without_verification() -> None:
    snapshot = await Held().snapshot()
    assert (snapshot.steps, snapshot.steps_title) == ((), "")


async def test_the_agents_running_are_listed_the_startup_check_first() -> None:
    held = Held(startup=FakeStartup(agent=STARTUP_AGENT), desk=FakeDesk((CODER_AGENT,)))
    assert (await held.snapshot()).agents == (STARTUP_AGENT, CODER_AGENT)


async def test_the_desks_agents_alone_are_listed_while_the_startup_check_isnt_one() -> None:
    held = Held(startup=FakeStartup(), desk=FakeDesk((CODER_AGENT,)))
    assert (await held.snapshot()).agents == (CODER_AGENT,)


async def test_without_agents_only_the_startup_check_can_be_listed() -> None:
    assert (await Held(startup=FakeStartup(agent=STARTUP_AGENT)).snapshot()).agents == (
        STARTUP_AGENT,
    )
    assert (await Held().snapshot()).agents == ()


async def test_each_lane_shows_whether_its_busy_by_name() -> None:
    seats = FakeSeats(lanes_busy={PLANNER_LANE: True})
    snapshot = await Held(turns=FakeTurns(running=True), seats=seats).snapshot()
    assert snapshot.busy == {WORK_LANE: True, PLANNER_LANE: True}


async def test_the_snapshot_shows_the_review_status_only_with_a_review_lane() -> None:
    status = ReviewStatus(paused=True, open_batches=2)
    assert (await Held(lane=FakeReviews(status=status)).snapshot()).review == status
    assert (await Held().snapshot()).review is None


async def test_the_snapshot_holds_the_open_sessions_own_parts() -> None:
    held = Held(
        seats=FakeSeats(names=(PLANNER, EXPERIMENTER), approvals=(APPROVAL,)),
        tool_calls=FakeHeldCalls((CALL,)),
    )
    held.settings.update(Settings(model="sonnet"), EXPERIMENTER)
    held.board.add(TaskSpec(prompt="Add a flag.", settings=Settings()))
    snapshot = await held.snapshot()
    assert snapshot.session == SessionSummary(id=OPEN, name="open", last_active=LAST_ACTIVE)
    assert held.switcher.asked == [LAST_ACTIVE]
    assert snapshot.role_settings == {PLANNER: Settings(), EXPERIMENTER: Settings(model="sonnet")}
    assert (snapshot.tasks, snapshot.approvals) == (held.board.tasks, (APPROVAL,))
    assert snapshot.tool_calls == (CALL,)
    assert (snapshot.proposals, snapshot.viewers) == ((), 2)


async def test_the_snapshot_holds_what_the_session_spent_by_role_and_by_asker() -> None:
    cost = Usage(cost_usd=1.5)
    spend = RoleSpend().charged(EXPERIMENTER, cost, PLANNER)
    snapshot = await Held(switcher=FakeSwitcher(spend=spend)).snapshot()
    assert snapshot.role_spent == {EXPERIMENTER: cost}
    assert snapshot.caused_spent == {PLANNER: {EXPERIMENTER: cost}}


async def test_the_snapshot_holds_the_open_sessions_latest_events_and_why_it_has_none() -> None:
    events = (EventEnvelope(seq=1, session=OPEN, payload=Notice(message="hi")),)
    error = ErrorOccurred(code=INTERNAL, message="The log couldn't be read.")
    held = Held(transcript=Transcript(events, error))
    shown = await held.shown()
    assert (shown.snapshot.transcript, shown.error, held.read) == (events, error, [OPEN])


async def test_the_state_shown_is_as_it_stands_once_the_transcript_is_read() -> None:
    held = Held()

    def starting() -> None:
        held.turns.running = True

    held.while_read = starting
    assert (await held.snapshot()).busy[WORK_LANE]


def test_the_entry_holds_the_sessions_in_the_states_a_connection_joining_sees() -> None:
    held = Held()
    summary = SessionSummary(id=OPEN, name="open")
    entry = held.entry(summary)
    assert entry == entry_of(sessions=(with_state(summary, OPEN_STATE),))
    assert held.states.joining == [True]


def test_the_entry_names_the_workspace_by_its_directory_and_says_how_its_held() -> None:
    entry = Held(workspace="/repos/orbit", read_only=True, managed=True).entry()
    assert entry == entry_of(label="orbit", path="/repos/orbit", read_only=True, managed=True)


def test_a_workspace_without_a_directory_has_the_default_label() -> None:
    assert Held().entry() == entry_of(label=NO_WORKSPACE_LABEL)


def test_the_entry_lists_the_agents_enabled_roles_those_with_a_seat_conversational() -> None:
    reviewer = ROLES["reviewer"].model_copy(update={"enabled": False})
    roster = FakeRoster(
        {**ROLES, "reviewer": reviewer}, frozenset({CODER}), frozenset({EXPERIMENTER})
    )
    seats = FakeSeats(names=(PLANNER, EXPERIMENTER))
    entry = Held(roster=roster, seats=seats).entry()
    enabled = {name: role for name, role in ROLES.items() if name != "reviewer"}
    expected = agent_entries(enabled, (PLANNER, EXPERIMENTER), {CODER}, {EXPERIMENTER})
    assert entry == entry_of(agents=expected)


def test_each_agent_entry_carries_its_roles_skills() -> None:
    entry = Held(roster=FakeRoster()).entry()
    [bugs] = [agent for agent in entry.agents if agent.name == BUGS_REVIEWER]
    assert bugs.skills == ("code-style", "review-bugs")


def test_the_entry_carries_the_skills_the_project_maps_in_order_each_once() -> None:
    mappings = (
        SkillMapping(paths=("src",), use=("application", "python")),
        SkillMapping(paths=("native",), use=("python", "cpp")),
    )
    entry = Held(roster=FakeRoster(skill_mappings=mappings)).entry()
    assert entry.mapped_skills == ("application", "python", "cpp")


def test_a_project_without_mappings_maps_no_skills() -> None:
    assert Held(roster=FakeRoster()).entry().mapped_skills == ()


def test_a_workspace_without_agents_maps_no_skills() -> None:
    assert Held().entry().mapped_skills == ()
