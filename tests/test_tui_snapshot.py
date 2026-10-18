"""The TUI replacing what it shows with a snapshot of the open session."""

from orbit.protocol import (
    PLANNER,
    PLANNER_LANE,
    WORK_LANE,
    AgentStarted,
    AssistantMessage,
    ChecksProposed,
    CommandAccepted,
    CommandRefused,
    ConversationReset,
    DecideProposal,
    Event,
    EventEnvelope,
    Notice,
    OpenRequest,
    ProposedStep,
    ReviewStatus,
    RunningAgent,
    SessionSummary,
    Settings,
    Snapshot,
    StepState,
    TaskSpec,
    TaskState,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)
from orbit.tui import OrbitApp
from orbit.tui.agents import AgentsPane
from orbit.tui.panels import VerificationPanel
from orbit.tui.proposals import ProposalScreen
from orbit.tui.tasks import TaskList
from orbit.tui.transcript import Transcript
from orbit.tui.view.agents import AgentRow
from orbit.tui.widgets import ChatPrompt
from tests.test_tui import PINNED, STRIP, FakeSession, entries, feed_events, opened, tab_strip

TASKS = (
    TaskState(id="1", task=TaskSpec(prompt="Add a flag."), status="running"),
    TaskState(id="2", task=TaskSpec(prompt="Document it.")),
)
PROPOSAL = ChecksProposed(
    agent_id="agent-3", task_id="1", steps=(ProposedStep(name="Lint", command=("ruff",)),)
)
CODER = RunningAgent(
    agent_id="agent-3",
    role="coder",
    task_id="1",
    open_requests=(OpenRequest(request_id="r1", tool="propose_checks"),),
)
SNAPSHOT = Snapshot(
    session=SessionSummary(
        id="s1", name="auth rework", usage=Usage(input_tokens=2_000, cost_usd=2.0)
    ),
    settings=Settings(model="opus"),
    model_choices=("opus", "sonnet"),
    busy={WORK_LANE: True, PLANNER_LANE: True},
    tasks=TASKS,
    steps=(StepState(name="Unit tests", status="running"),),
    steps_title="Startup check",
    review=ReviewStatus(paused=True, open_batches=0),
    proposals=(),
    agents=(CODER,),
)


async def test_a_snapshot_shows_its_tasks_busy_planner_agents_and_steps() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT)
        task_list = app.query_one(TaskList)
        assert task_list.tasks == TASKS
        assert task_list.border_subtitle == "session: 2k tok · $2.00"
        prompt = app.query_one(ChatPrompt)
        assert prompt.border_title == "Prompt · planner · busy"
        assert prompt.border_subtitle == "planner's model: opus · new tasks' tools: restricted"
        pane = app.query_one(AgentsPane)
        assert pane.rows == (AgentRow("agent-3", "coder", "1", (("r1", "propose_checks"),)),)
        assert pane.border_subtitle == "review: paused"
        panel = app.query_one(VerificationPanel)
        assert (panel.steps, panel.title) == (SNAPSHOT.steps, "Startup check")
        assert str(app.query_one(Transcript).border_title) == "Conversation — auth rework"


async def test_a_snapshot_replaces_the_agents_and_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        started = AgentStarted(agent_id="agent-1", role="planner", task_id="plan")
        await feed_events(session, pilot, started, Notice(message="Workspace: /work"))
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"agents": ()}))
        assert app.query_one(AgentsPane).rows == ()
        assert entries(app) == []
        assert app.query_one(Transcript).entries(PLANNER) == ()


def logged(*events: Event) -> tuple[EventEnvelope, ...]:
    """``events`` as a snapshot's transcript, numbered from 1."""
    return tuple(
        EventEnvelope(seq=seq, session="s1", payload=event)
        for seq, event in enumerate(events, start=1)
    )


PLANNER_TURN = (
    TurnStarted(turn_id="p1", agent_id=PLANNER),
    AssistantMessage(turn_id="p1", agent_id=PLANNER, text="Two tasks, then."),
    TurnFinished(turn_id="p1", agent_id=PLANNER, reason="completed"),
)
CODER_CALL = (
    AgentStarted(agent_id="agent-3", role="coder", task_id="1"),
    ToolCallStarted(turn_id="c1", agent_id="agent-3", call_id="x", name="Read", arguments={}),
    ToolCallFinished(turn_id="c1", agent_id="agent-3", call_id="x", output="text"),
)


def texts(app: OrbitApp, key: str | None) -> list[str]:
    """The texts of the entries under the tab keyed ``key``."""
    return [entry.text for entry in app.query_one(Transcript).entries(key)]


async def test_a_snapshots_transcript_shows_its_turns_and_tool_calls_in_their_tabs() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    tail = SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, tail)
        assert "Two tasks, then." in texts(app, PLANNER)
        [call] = [entry for entry in app.query_one(Transcript).entries("coder") if entry.call]
        assert call.call is not None and call.call.status == "passed"
        assert texts(app, None)[0] == "Two tasks, then."


async def test_a_later_snapshot_of_another_session_replaces_the_transcript() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    tail = SNAPSHOT.model_copy(update={"transcript": logged(*PLANNER_TURN, *CODER_CALL)})
    other = opened(SessionSummary(id="s2", name="docs"))
    note = Notice(message="Notes on docs.")
    async with app.run_test() as pilot:
        await feed_events(session, pilot, tail)
        await feed_events(session, pilot, other.model_copy(update={"transcript": logged(note)}))
        assert texts(app, None) == ["📌 Notes on docs."]
        assert texts(app, PLANNER) == []
        assert texts(app, "coder") == []
        assert str(app.query_one(Transcript).border_title) == "Conversation — docs"


async def test_a_snapshot_closes_the_agent_tabs_its_transcript_doesnt_open() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    running = RunningAgent(agent_id="agent-4", role="checker", task_id="1")
    empty = opened(SessionSummary(id="s1", name="auth"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, checker)
        assert tab_strip(app) == [*STRIP, "checker"]
        await feed_events(session, pilot, empty)
        assert tab_strip(app) == STRIP
        reopened = empty.model_copy(update={"transcript": logged(checker), "agents": (running,)})
        await feed_events(session, pilot, reopened)
        assert tab_strip(app) == [*STRIP, "checker"]


async def test_a_snapshots_agents_started_but_not_running_get_no_tab_or_marker() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = AgentStarted(agent_id="agent-4", role="checker", task_id="1")
    crashed = opened(SessionSummary(id="s1", name="auth")).model_copy(
        update={"transcript": logged(*CODER_CALL, checker)}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, crashed)
        assert tab_strip(app) == ["All", "planner", "coder *", *PINNED[2:]]
        assert "Read" in "".join(texts(app, "coder"))


async def test_a_running_agent_started_before_the_transcript_gets_its_roles_tab() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    checker = RunningAgent(agent_id="agent-4", role="checker", task_id="2")
    cut = SNAPSHOT.model_copy(
        update={"agents": (CODER, checker), "transcript": logged(*CODER_CALL[1:])}
    )
    async with app.run_test() as pilot:
        await feed_events(session, pilot, cut)
        assert tab_strip(app) == ["All", "● planner", "● coder *", *PINNED[2:], "checker"]
        [call] = [entry for entry in app.query_one(Transcript).entries("coder") if entry.call]
        assert call.call is not None and call.call.status == "passed"
        assert texts(app, "agent-3") == []


async def test_a_snapshots_transcript_shows_a_reset_and_a_refusal_as_they_were_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    refused = CommandRefused(command_id="c", code="busy", message="Task 1 is running.")
    tail = logged(ConversationReset(agent=PLANNER), refused, CommandAccepted(command_id="d"))
    async with app.run_test() as pilot:
        await feed_events(session, pilot, ConversationReset(agent=PLANNER), refused)
        live = entries(app)
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"transcript": tail}))
        assert entries(app) == live


async def test_a_snapshot_with_a_waiting_proposal_shows_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        assert screen.proposal == PROPOSAL


async def test_a_snapshot_still_waiting_on_the_shown_proposal_keeps_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    waiting = SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        shown = app.screen
        await feed_events(session, pilot, waiting)
        assert app.screen is shown


async def test_a_snapshot_without_the_shown_proposal_closes_it() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        assert isinstance(app.screen, ProposalScreen)
        await feed_events(session, pilot, SNAPSHOT)
        assert not isinstance(app.screen, ProposalScreen)
        assert session.commands == []


async def test_a_snapshot_with_another_proposal_shows_that_one_in_place_of_the_shown() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, PROPOSAL)
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (other,)}))
        screen = app.screen
        assert isinstance(screen, ProposalScreen)
        assert screen.proposal == other
        await pilot.press("a")
        await pilot.pause()
        assert not isinstance(app.screen, ProposalScreen)


def proposal_screens(app: OrbitApp) -> list[ProposalScreen]:
    """The proposal screens on ``app``'s stack."""
    return [screen for screen in app.screen_stack if isinstance(screen, ProposalScreen)]


async def test_the_proposal_event_after_a_snapshot_showing_it_adds_no_second_screen() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        await feed_events(session, pilot, PROPOSAL)
        assert len(proposal_screens(app)) == 1
        await pilot.press("a")
        await pilot.pause()
        assert session.commands == [DecideProposal(task_id="1", action="accept")]
        assert proposal_screens(app) == []


async def test_another_proposal_event_while_one_is_shown_shows_its_own_screen() -> None:
    session = FakeSession()
    app = OrbitApp(session)
    other = PROPOSAL.model_copy(update={"task_id": "2"})
    async with app.run_test() as pilot:
        await feed_events(session, pilot, SNAPSHOT.model_copy(update={"proposals": (PROPOSAL,)}))
        await feed_events(session, pilot, other)
        assert [screen.proposal for screen in proposal_screens(app)] == [PROPOSAL, other]
